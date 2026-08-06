"""Deterministic selection, one-call AI analysis and light verification."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.ai.client import ItemAnalysisClient
from app.ai.schemas import ItemAnalysisRequest, ItemAnalysisResponse
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, SourceConfig, load_source_registry
from app.domain.models import FetchItem, SourceSpec
from app.domain.policies import selection_decision, source_spec_from_config
from app.domain.verification import (
    MODE_DISCOVERY,
    MODE_METADATA,
    MODE_OFFICIAL,
    STATUS_SKIPPED,
    VerificationResult,
    domain_from_url,
    verify_item,
)
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelCounts, IntelRepository
from app.storage.models import IntelItem, Source

LOGGER = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_OFFICIAL_DOMAINS = {"openai.com", "anthropic.com", "deepmind.google", "ai.google", "huggingface.co", "mistral.ai", "cohere.com", "deepseek.com", "qwen.ai"}


@dataclass
class IntelProcessResult:
    processed: int = 0
    selected: int = 0
    filtered: int = 0
    analyzed: int = 0
    verified: int = 0
    needs_review: int = 0
    failed: int = 0
    ai_failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_intel_process_job(
    *,
    session_factory: sessionmaker[Session],
    source_specs: Mapping[str, SourceSpec] | None = None,
    ai_client: ItemAnalysisClient | Any | None = None,
    http_client: Any | None = None,
    limit: int | None = 100,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    verification_timeout_seconds: float = 10.0,
) -> IntelProcessResult:
    result = IntelProcessResult()
    source_specs = dict(source_specs or {})
    with session_factory() as session:
        repo = IntelRepository(session)
        # Rank the complete pending pool before applying the work limit.  A
        # collector's page order is not reliable for persisted Product Hunt or
        # community items, and a low limit must not hide a higher-signal item.
        pending_items = repo.list_pending_items(
            limit=None,
            content_class=content_class,
            source_id=source_filter,
            force=force,
        )
        ranked_items: list[tuple[IntelItem, Any, SourceSpec]] = []
        for pending_item in pending_items:
            pending_spec = source_specs.get(pending_item.source_id) or _spec_from_row(pending_item.source)
            pending_decision = selection_decision(_item_to_fetch_item(pending_item), pending_spec)
            ranked_items.append((pending_item, pending_decision, pending_spec))
        ranked_items.sort(key=_ranking_key, reverse=True)
        if limit is not None:
            ranked_items = ranked_items[:limit]

        for item, precomputed_decision, precomputed_spec in ranked_items:
            result.processed += 1
            spec = precomputed_spec
            fetch_item = _item_to_fetch_item(item)
            analysis_saved = False
            try:
                discovered_links = _discover_links(fetch_item) if spec.content_class == "community_social" else []
                if discovered_links and not dry_run:
                    repo.save_discovered_links(item.id, discovered_links)
                    session.commit()
                decision = precomputed_decision
                if dry_run:
                    if decision.selected:
                        result.selected += 1
                    else:
                        result.filtered += 1
                    continue

                repo.save_selection(
                    item.id,
                    keep=decision.selected,
                    # GitHub rows have no composite score. Their persisted
                    # order is supplied by stars/forks/pushed_at metadata.
                    score=0 if _is_github_source(spec) else round(decision.score),
                    reason=_selection_reason(decision),
                )
                session.commit()
                if not decision.selected:
                    result.filtered += 1
                    continue
                result.selected += 1

                # GitHub repository/release sources are already structured
                # metadata. Their inclusion decision is deterministic
                # (stars, recent push and source policy); do not spend an AI
                # call producing a second score or gate. Keep the metadata
                # verification row for auditable repository facts.
                if _is_github_source(spec):
                    verification = verify_item(
                        fetch_item,
                        {"content_class": "project_tool"},
                    )
                    repo.upsert_verification(item.id, verification)
                    repo.set_item_status(item.id, "hotspot")
                    session.commit()
                    continue

                if ai_client is None:
                    raise RuntimeError("item analysis client is not configured")
                request = ItemAnalysisRequest(
                    item_id=item.id,
                    title=item.title,
                    url=item.canonical_url,
                    source_id=item.source_id,
                    source_content_class=spec.content_class or "community_social",
                    body_preview=(item.content_text or item.summary or "")[:8000],
                    metrics={**_json_dict(item.metrics_json), "discovered_links": discovered_links},
                )
                response = ai_client.analyze(request)
                if not isinstance(response, ItemAnalysisResponse):
                    response = _coerce_analysis_response(response, request.source_content_class)
                # Registry/source policy is authoritative for routing. The model
                # may suggest a class, but cannot move an item across streams.
                extra_risks = [f"link_candidate:{entry['content_class']}" for entry in discovered_links]
                extra_risks.extend(f"selection:{flag}" for flag in decision.risk_flags)
                response = response.model_copy(
                    update={
                        "content_class": spec.content_class or response.content_class,
                        "risk_flags": list(dict.fromkeys([*response.risk_flags, *extra_risks])),
                    }
                )
                repo.upsert_ai_review(
                    item.id,
                    response,
                    model=getattr(ai_client, "model", None),
                    content_class=spec.content_class,
                )
                result.analyzed += 1
                # Commit the model result before the optional HTTP check. A
                # verifier failure must not erase a successful AI analysis.
                session.commit()
                analysis_saved = True

                if response.keep:
                    verification = _verify(
                        item,
                        fetch_item,
                        response,
                        spec,
                        http_client=http_client,
                        timeout_seconds=verification_timeout_seconds,
                    )
                    repo.upsert_verification(item.id, verification)
                    final_status = _final_status(response, verification, spec)
                    repo.set_item_status(item.id, final_status)
                    if final_status == "verified":
                        result.verified += 1
                    if final_status == "needs_review":
                        result.needs_review += 1
                else:
                    # An official item without any usable direct link remains
                    # auditable as ``needs_review`` even when the model cannot
                    # confidently keep it.  It must not become a strong
                    # rejection merely because the optional link is absent.
                    if (
                        spec.content_class == "official_model_company"
                        and not response.official_url
                        and not item.canonical_url
                    ):
                        repo.set_item_status(item.id, "needs_review")
                        repo.upsert_verification(item.id, _skipped_result(spec, reason="missing_official_url"))
                        result.needs_review += 1
                    else:
                        repo.set_item_status(item.id, "rejected")
                        repo.upsert_verification(item.id, _skipped_result(spec, reason="ai_keep_false"))
                session.commit()
            except Exception as exc:
                result.failed += 1
                error = f"intel_item_id={item.id}: {exc}"
                result.errors.append(error)
                LOGGER.exception("Failed to process intel item %s", item.id)
                if not dry_run:
                    try:
                        session.rollback()
                        if analysis_saved:
                            # The AI row is already committed; persist a
                            # conservative verification failure separately.
                            repo.upsert_verification(item.id, _failed_verification(spec, str(exc)))
                            repo.set_item_status(item.id, "needs_review")
                            result.needs_review += 1
                        elif _is_github_source(spec):
                            # A metadata verifier failure is not an AI failure;
                            # keep the repository row auditable without
                            # creating an artificial ai_item_reviews record.
                            repo.upsert_verification(item.id, _failed_verification(spec, str(exc)))
                            repo.set_item_status(item.id, "needs_review")
                            result.needs_review += 1
                        else:
                            # Commit the failure independently so a slow/broken
                            # model response cannot roll back earlier items.
                            repo.upsert_ai_review(
                                item.id,
                                None,
                                model=getattr(ai_client, "model", None) if ai_client is not None else None,
                                content_class=spec.content_class,
                                status="ai_failed",
                                error_message=str(exc),
                            )
                            repo.set_item_status(item.id, "ai_failed")
                            result.ai_failed += 1
                        session.commit()
                    except Exception:
                        session.rollback()
                        LOGGER.exception("Failed to persist AI error for intel item %s", item.id)
    return result


def _ranking_key(entry: tuple[IntelItem, Any, SourceSpec]) -> tuple[int, float, float, float, int, float, int]:
    """Apply explicit per-class ordering before the processing limit."""

    item, decision, spec = entry
    published = item.published_at
    if published is None:
        timestamp = float("-inf")
    else:
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        timestamp = published.timestamp()
    metrics = _json_dict(item.metrics_json)
    primary = float(decision.score)
    secondary = 0.0
    mode = spec.selection_policy.mode.casefold().replace("-", "_")
    if spec.content_class == "project_tool":
        linked_github = bool(
            metrics.get("github_url")
            or metrics.get("canonical_project_key")
            or metrics.get("github_metadata_fetched")
        )
        trending = metrics.get("trending") if isinstance(metrics.get("trending"), dict) else {}
        weekly_trending = trending.get("weekly") if isinstance(trending.get("weekly"), dict) else {}
        daily_trending = trending.get("daily") if isinstance(trending.get("daily"), dict) else {}
        if mode == "github_trending" or trending:
            # A repository can first arrive through Search API and later be
            # refreshed by Trending. Prefer the merged period signal whenever
            # it exists, regardless of which source created the row.
            period_signal = max(
                _number(metrics.get("stars_since") or metrics.get("trending_stars")),
                _number(weekly_trending.get("stars_since")),
                _number(daily_trending.get("stars_since")),
            )
            primary = period_signal
            secondary = _number(metrics.get("stars") or metrics.get("stargazers_count"))
        elif mode in {"github_active_high_star", "active_high_star"} or linked_github:
            primary = _number(metrics.get("stars") or metrics.get("stargazers_count"))
            secondary = _number(metrics.get("forks") or metrics.get("forks_count"))
        elif mode in {"producthunt_hot", "product_hunt_hot"}:
            primary = _number(metrics.get("votes") or metrics.get("vote_count") or metrics.get("upvotes"))
            secondary = _number(metrics.get("comments") or metrics.get("comments_count"))
    elif spec.content_class == "official_model_company":
        # Official entries are ordered by publication time first; score is a
        # later tie-breaker for direct-link/keyword quality.
        primary = timestamp

    # Reverse sort makes higher stars/votes/newness win, lower registry
    # priority win ties, and the oldest row id provide a stable final tie-break.
    signal = 0.0 if _is_github_source(spec) else float(decision.score)
    return (
        1 if decision.selected else 0,
        primary,
        secondary,
        timestamp,
        -int(getattr(spec, "priority", 100)),
        signal,
        -int(item.id),
    )


def _selection_reason(decision: Any) -> str:
    reason = str(decision.reason or "")
    flags = [str(flag) for flag in (decision.risk_flags or ()) if str(flag)]
    return reason if not flags else f"{reason}; risks={','.join(flags)}"


def _is_github_source(spec: SourceSpec) -> bool:
    """Return whether a source is a first-party GitHub metadata source."""

    mode = spec.selection_policy.mode.casefold().replace("-", "_")
    return bool(
        spec.type in {"github_api", "github_trending"}
        or spec.collector_type in {"github", "github_trending"}
        or mode in {"github_active_high_star", "active_high_star", "github_trending"}
    )


def run_intel_process_from_settings(
    *,
    settings: Settings,
    registry_path=DEFAULT_REGISTRY_PATH,
    limit: int | None = 100,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    http_client: Any | None = None,
) -> IntelProcessResult:
    registry = load_source_registry(registry_path, env={"RSSHUB_BASE_URL": settings.rsshub_base_url or ""})
    specs = {source.id: source_spec_from_config(source) for source in registry.sources}
    database_url = _readable_database_url(settings.database_url, dry_run=dry_run)
    engine = create_engine_from_url(database_url)
    if not dry_run or database_url == "sqlite:///:memory:":
        init_db(engine)
    session_factory = create_session_factory(engine)
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True, headers={"User-Agent": settings.user_agent})
    try:
        ai_client = ItemAnalysisClient.from_settings(settings, http_client=client)
        return run_intel_process_job(
            session_factory=session_factory,
            source_specs=specs,
            ai_client=ai_client,
            http_client=client,
            limit=limit,
            source_filter=source_filter,
            content_class=content_class,
            force=force,
            dry_run=dry_run,
            verification_timeout_seconds=settings.request_timeout_seconds,
        )
    finally:
        if own_client:
            client.close()


def _verify(
    item: IntelItem,
    fetch_item: FetchItem,
    response: ItemAnalysisResponse,
    spec: SourceSpec,
    *,
    http_client: Any | None,
    timeout_seconds: float,
) -> VerificationResult:
    policy_mode = spec.verification_policy.mode if spec.verification_policy else None
    if policy_mode == MODE_DISCOVERY or spec.content_class == "community_social":
        return verify_item(fetch_item, response)
    if policy_mode == MODE_METADATA and spec.content_class == "official_model_company":
        return _skipped_result(spec, reason="metadata_only_policy", mode=MODE_METADATA)
    if spec.content_class == "project_tool":
        if policy_mode == MODE_OFFICIAL:
            allowed = [domain_from_url(item.canonical_url), domain_from_url(spec.url), *_OFFICIAL_DOMAINS]
            direct_response = response.model_copy(
                update={"content_class": "official_model_company", "needs_verification": True}
            )
            direct_result = verify_item(
                fetch_item,
                direct_response,
                http_client=http_client,
                timeout_seconds=timeout_seconds,
                allowed_domains=[domain for domain in allowed if domain],
            )
            return replace(direct_result, content_class="project_tool")
        return verify_item(fetch_item, response, http_client=http_client)
    allowed = [domain_from_url(item.canonical_url), domain_from_url(spec.url)]
    # Official release/model-card links may legitimately live on GitHub or
    # Hugging Face even when the discovery feed is hosted elsewhere.
    allowed.extend(["github.com", "huggingface.co", *_OFFICIAL_DOMAINS])
    allowed = [domain for domain in allowed if domain]
    return verify_item(
        fetch_item,
        response,
        http_client=http_client,
        timeout_seconds=timeout_seconds,
        allowed_domains=allowed or None,
    )


def _final_status(response: ItemAnalysisResponse, verification: VerificationResult, spec: SourceSpec) -> str:
    if not response.keep:
        return "rejected"
    if spec.content_class == "project_tool":
        return "hotspot"
    if spec.content_class == "community_social":
        return "discovery_only"
    return "verified" if verification.status == "verified" and verification.supports_basic_fact else "needs_review"


def _skipped_result(spec: SourceSpec, *, reason: str, mode: str | None = None) -> VerificationResult:
    resolved_mode = (
        spec.verification_policy.mode
        if spec.verification_policy is not None
        else MODE_DISCOVERY
    )
    if spec.content_class == "project_tool":
        resolved_mode = MODE_METADATA
    elif spec.content_class == "community_social":
        resolved_mode = MODE_DISCOVERY
    else:
        resolved_mode = MODE_OFFICIAL
    return VerificationResult(
        status=STATUS_SKIPPED,
        mode=mode or resolved_mode,
        content_class=spec.content_class or "community_social",
        reason=reason,
        risk_flags=[reason],
    )


def _failed_verification(spec: SourceSpec, reason: str) -> VerificationResult:
    result = _skipped_result(spec, reason=reason)
    return replace(result, status="failed", risk_flags=["verification_error"])


def _item_to_fetch_item(item: IntelItem) -> FetchItem:
    return FetchItem(
        item_id=item.id,
        source_id=item.source_id,
        content_class=item.content_class,
        external_id=item.external_id,
        title=item.title,
        url=item.canonical_url,
        published_at=item.published_at,
        captured_at=item.captured_at,
        summary=item.summary,
        content=item.content_text,
        metrics=_json_dict(item.metrics_json),
        raw_payload=_json_dict(item.raw_payload_json),
    )


def _spec_from_row(row: Source | None) -> SourceSpec:
    if row is None:
        return source_spec_from_config({"id": "unknown", "name": "unknown", "type": "rss"})
    return source_spec_from_config(
        {
            "id": row.id,
            "name": row.name,
            "type": row.type,
            "url": row.url,
            "enabled": row.enabled,
            "priority": row.priority,
            "fetch_interval": row.fetch_interval,
            "source_group": row.source_group,
            "source_subtype": row.source_subtype,
            "source_role": row.source_role,
            "content_class": row.content_class,
            "selection_policy": _json_dict(row.selection_policy_json),
            "verification_policy": _json_dict(row.verification_policy_json),
        }
    )


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        text = str(value).replace(",", "").strip().casefold()
        multiplier = 1.0
        if text.endswith("k"):
            multiplier, text = 1_000.0, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1_000_000.0, text[:-1]
        return max(0.0, float(text) * multiplier)
    except (TypeError, ValueError):
        return 0.0


def _coerce_analysis_response(value: Any, source_class: str) -> ItemAnalysisResponse:
    if isinstance(value, Mapping):
        from app.ai.schemas import parse_item_analysis_response

        return parse_item_analysis_response(value, source_class)
    raise TypeError("AI client returned an unsupported response")


def _discover_links(item: FetchItem) -> list[dict[str, str]]:
    """Turn links found in community text into auditable follow-up candidates."""

    material = "\n".join(
        str(value)
        for value in (item.title, item.summary, item.content, json.dumps(item.raw_payload, ensure_ascii=False, default=str))
        if value
    )
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(material):
        url = raw.rstrip(".,);]}")
        if url in seen:
            continue
        seen.add(url)
        domain = domain_from_url(url) or ""
        path = url.split(domain, 1)[-1].casefold() if domain in url else url.casefold()
        if domain == "github.com" and len([part for part in path.split("/") if part]) >= 2:
            target = "project_tool"
        elif domain in _OFFICIAL_DOMAINS or any(token in path for token in ("model-card", "/models/", "/docs/", "/release")):
            target = "official_model_company"
        else:
            continue
        links.append({"url": url, "content_class": target})
    return links


def _readable_database_url(database_url: str, *, dry_run: bool) -> str:
    if not dry_run or not database_url.startswith("sqlite:///"):
        return database_url
    path_text = database_url[len("sqlite:///") :]
    if path_text in {":memory:", ""}:
        return "sqlite:///:memory:"
    path = Path(path_text)
    if not path.is_absolute():
        path = Path.cwd() / path
    return database_url if path.exists() else "sqlite:///:memory:"
