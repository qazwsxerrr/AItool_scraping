"""Persistence boundary for the simplified intelligence pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.domain.models import SourceSpec
from app.storage.models import (
    AIItemReview,
    IntelItem,
    IntelItemVerification,
    IntelRun,
    Source,
    FetchAttempt,
    utcnow,
)


@dataclass(frozen=True)
class IntelInsertResult:
    inserted: bool
    item_id: int | None = None
    reason: str | None = None
    updated: bool = False


@dataclass(frozen=True)
class IntelCounts:
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    selected: int = 0
    analyzed: int = 0
    verified: int = 0
    failed: int = 0


class IntelRepository:
    """Small, explicit write/read API used by v2 jobs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_source(self, source: SourceSpec, *, policy: Any | None = None) -> Source:
        """Persist one resolved ``SourceSpec`` without reconstructing config.

        The nested feed/GitHub options are intentionally stored as explicit
        columns. This keeps source rows useful to the fetch cooldown checks and
        lets a fresh local database be inspected without legacy routing fields.
        """
        row = self.session.get(Source, source.id)
        if row is None:
            row = Source(id=source.id)
            self.session.add(row)
        row.name = source.name or source.id
        row.transport = source.transport
        row.url = source.url
        row.enabled = source.enabled
        row.priority = source.priority
        row.fetch_interval = source.fetch_interval
        row.default_limit = source.default_limit
        feed = source.feed
        github = source.github
        row.feed_format = getattr(feed, "format", None) if feed is not None else None
        row.feed_adapter = getattr(feed, "adapter", None) if feed is not None else None
        row.github_mode = getattr(github, "mode", None) if github is not None else None
        row.github_query = getattr(github, "query", None) if github is not None else None
        row.github_sort = getattr(github, "sort", None) if github is not None else None
        row.github_order = getattr(github, "order", None) if github is not None else None
        row.github_pushed_days = getattr(github, "pushed_days", None) if github is not None else None
        row.github_period = getattr(github, "period", None) if github is not None else None
        row.source_group = source.source_group or "general"
        row.source_subtype = source.source_subtype or "fixed"
        row.quality_weight = source.quality_weight
        row.source_role = source.source_role
        row.spam_risk = source.spam_risk
        row.requires_verification = source.requires_verification
        row.content_class = source.content_class or "community_social"
        selection_policy = getattr(source, "selection_policy", {})
        verification_policy = getattr(source, "verification_policy", {})
        if hasattr(selection_policy, "model_dump"):
            selection_policy = selection_policy.model_dump(exclude_none=True)
        if hasattr(verification_policy, "model_dump"):
            verification_policy = verification_policy.model_dump(exclude_none=True)
        row.selection_policy_json = _dump_json(selection_policy or {})
        row.verification_policy_json = _dump_json(verification_policy or {})
        if policy is not None:
            # Resolved ``SourceSpec`` instances normally provide both values,
            # but callers may pass a lightweight policy object. Preserve the
            # source defaults so a missing optional attribute cannot violate
            # the non-null schema on commit.
            row.content_class = (
                _text(getattr(policy, "content_class", None))
                or source.content_class
                or row.content_class
                or "community_social"
            )
            row.selection_policy_json = _dump_json(_policy_dict(policy, "selection"))
            row.verification_policy_json = _dump_json(_policy_dict(policy, "verification"))
        return row

    def start_run(self, *, filters: Mapping[str, Any] | None = None) -> IntelRun:
        run = IntelRun(status="running", filters_json=_dump_json(dict(filters or {})))
        self.session.add(run)
        self.session.flush()
        return run

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        counts: IntelCounts | Mapping[str, int] | None = None,
        error: str | None = None,
    ) -> None:
        run = self.session.get(IntelRun, run_id)
        if run is None:
            return
        values = _counts_dict(counts)
        run.status = status
        run.finished_at = utcnow()
        for name in ("fetched", "inserted", "selected", "analyzed", "verified", "failed"):
            if name in values:
                setattr(run, name, int(values[name]))
        run.error = error[:4000] if error else None

    def create_attempt(
        self,
        *,
        source_id: str,
        request_url: str,
        run_id: int | None = None,
        manual_override: bool = False,
    ) -> FetchAttempt:
        attempt = FetchAttempt(
            source_id=source_id,
            run_id=run_id,
            request_url=request_url,
            manual_override=manual_override,
            started_at=utcnow(),
            status="running",
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        metadata: Any | None = None,
        items_fetched: int = 0,
        items_inserted: int = 0,
        items_skipped: int = 0,
        error: Exception | str | None = None,
    ) -> None:
        attempt = self.session.get(FetchAttempt, attempt_id)
        if attempt is None:
            return
        attempt.status = status
        attempt.finished_at = utcnow()
        attempt.items_fetched = items_fetched
        attempt.items_inserted = items_inserted
        attempt.items_skipped = items_skipped
        if metadata is not None:
            attempt.transport = getattr(metadata, "transport", None) or attempt.transport
            request_url = getattr(metadata, "request_url", None)
            if request_url:
                attempt.request_url = str(request_url)[:2000]
            attempt.final_url = getattr(metadata, "final_url", None)
            attempt.http_status = getattr(metadata, "http_status", None)
            if attempt.http_status is None:
                attempt.http_status = getattr(metadata, "status_code", None)
            attempt.response_bytes = int(getattr(metadata, "response_bytes", 0) or 0)
            attempt.retry_count = int(getattr(metadata, "retry_count", 0) or 0)
            attempt.error_code = getattr(metadata, "error_code", None)
        if error is not None:
            message = str(error)
            if isinstance(error, str) and error:
                # Callers use short stable error codes for scheduler skips;
                # preserve those instead of recording the generic ``str``.
                candidate_code = error.split(":", 1)[0].strip()
                attempt.error_code = attempt.error_code or (candidate_code[:64] if candidate_code else "fetch_failed")
            else:
                attempt.error_code = getattr(error, "error_code", None) or attempt.error_code or type(error).__name__.lower()
            attempt.error_message = message[:4000]

    def insert_item(self, item: Any) -> IntelInsertResult:
        """Insert or refresh one normalized item idempotently.

        GitHub repository identifiers are stable across search sources, so they
        are deduplicated globally. Other sources use source + external id,
        canonical URL, and finally the content hash.
        """

        fields = _item_fields(item)
        # Collectors may omit the class because the source registry is the
        # authoritative routing boundary. Resolve that class before dedupe and
        # persistence so GitHub rows are not accidentally stored as community
        # items when a lightweight DTO omits it.
        if not fields["content_class"]:
            source = self.session.get(Source, fields["source_id"])
            if source is None:
                # ``upsert_source`` may still be pending in the same
                # transaction (as in direct job/test invocations).
                self.session.flush()
                source = self.session.get(Source, fields["source_id"])
            fields["content_class"] = source.content_class if source is not None else "community_social"
        if fields["content_class"] == "project_tool":
            github_url = _canonical_github_url(fields.get("canonical_url"))
            if github_url and (str(fields.get("external_id") or "").casefold().startswith("github_repo:")):
                fields["canonical_url"] = github_url
        existing = self._find_existing(fields)
        if existing is not None:
            # Refresh volatile metrics/payload while preserving a completed
            # processing status. This lets a later fetch update star counts.
            old_metrics = existing.metrics_json
            old_payload = existing.raw_payload_json
            merged_metrics = (
                _merge_github_project_metrics(
                    _load_json(old_metrics, {}),
                    fields["metrics"],
                    source_id=fields["source_id"],
                )
                if _is_github_repository_fields(fields)
                else fields["metrics"]
            )
            new_metrics = _dump_json(merged_metrics)
            new_payload = _dump_json(
                _merge_github_raw_payload(_load_json(old_payload, {}), fields["raw_payload"])
                if _is_github_repository_fields(fields)
                else fields["raw_payload"]
            )
            class_changed = existing.content_class != fields["content_class"]
            existing.title = fields["title"] or existing.title
            existing.summary = fields["summary"] or existing.summary
            existing.content_text = fields["content_text"] or existing.content_text
            existing.canonical_url = fields["canonical_url"] or existing.canonical_url
            existing.published_at = fields["published_at"] or existing.published_at
            existing.content_class = fields["content_class"]
            existing.metrics_json = new_metrics
            existing.raw_payload_json = new_payload
            if (class_changed or old_metrics != new_metrics or old_payload != new_payload) and existing.status in {
                "hotspot",
                "verified",
                "discovery_only",
                "rejected",
                "filtered",
                "needs_review",
                "ai_failed",
            }:
                # A refreshed item must pass deterministic selection again. Its
                # previous AI result remains available for audit until process
                # replaces it.
                existing.status = "new"
                existing.selection_score = 0
                existing.selection_reason = None
            existing.updated_at = utcnow()
            return IntelInsertResult(inserted=False, item_id=existing.id, reason="duplicate", updated=True)

        row_metrics = (
            _merge_github_project_metrics({}, fields["metrics"], source_id=fields["source_id"])
            if _is_github_repository_fields(fields)
            else fields["metrics"]
        )
        row = IntelItem(
            source_id=fields["source_id"],
            external_id=fields["external_id"],
            canonical_url=fields["canonical_url"],
            title=fields["title"] or "(untitled)",
            summary=fields["summary"],
            content_text=fields["content_text"],
            published_at=fields["published_at"],
            captured_at=fields["captured_at"],
            content_class=fields["content_class"],
            metrics_json=_dump_json(row_metrics),
            raw_payload_json=_dump_json(fields["raw_payload"]),
            content_hash=fields["content_hash"],
            status="new",
        )
        self.session.add(row)
        self.session.flush()
        return IntelInsertResult(inserted=True, item_id=row.id)

    def save_github_enrichment(self, item_id: int, enrichment: Mapping[str, Any]) -> IntelItem | None:
        """Persist bounded repository metadata/README enrichment in JSON fields.

        GitHub rows already carry all required storage columns.  This method
        merges enrichment into metrics/raw payload while preserving period Star
        signals and source provenance, so reruns remain idempotent.
        """

        item = self.session.get(IntelItem, item_id)
        if item is None:
            return None
        metadata = enrichment.get("metadata") if isinstance(enrichment, Mapping) else {}
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        metrics = _load_json(item.metrics_json, {})
        metadata_metrics = _github_metadata_metrics(metadata)
        metadata_metrics["github_enrichment_status"] = "partial" if enrichment.get("errors") else "success"
        metadata_metrics["github_enrichment_errors"] = [str(error) for error in enrichment.get("errors", []) if error]
        metadata_metrics["readme_checked"] = bool(enrichment.get("readme_checked"))
        if enrichment.get("readme_checked"):
            metadata_metrics["readme_present"] = enrichment.get("readme_present")
            metadata_metrics["has_readme"] = enrichment.get("readme_present")
        merged_metrics = _merge_github_project_metrics(metrics, metadata_metrics, source_id=item.source_id)

        readme_text = _text(enrichment.get("readme_text"))
        if readme_text:
            merged_metrics["readme_chars"] = len(readme_text)
            item.content_text = readme_text
        elif not metrics.get("readme_chars") and (
            enrichment.get("readme_checked")
            or "readme_text" in enrichment
            or enrichment.get("errors")
        ):
            # GitHub collectors use the normalized content field for a compact
            # metrics payload before enrichment. Never expose that JSON as a
            # README when the bounded README lookup did not produce text.
            item.content_text = None
        description = _text(metadata.get("description"))
        if description:
            item.summary = description
        item.metrics_json = _dump_json(merged_metrics)

        raw_payload = _load_json(item.raw_payload_json, {})
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        raw_payload = _merge_github_raw_payload(raw_payload, _bounded_github_metadata(metadata))
        raw_payload["github_enrichment"] = {
            "metadata_fetched": bool(metadata),
            "readme_checked": bool(enrichment.get("readme_checked")),
            "readme_present": enrichment.get("readme_present"),
            "readme_text": readme_text,
            "errors": [str(error) for error in enrichment.get("errors", []) if error],
        }
        item.raw_payload_json = _dump_json(raw_payload)
        item.updated_at = utcnow()
        self.session.flush()
        return item

    def _find_existing(self, fields: Mapping[str, Any]) -> IntelItem | None:
        external_id = fields.get("external_id")
        if external_id:
            stmt = select(IntelItem).where(
                IntelItem.source_id == fields["source_id"],
                IntelItem.external_id == external_id,
            )
            found = self.session.scalar(stmt)
            if found is not None:
                return found
            if str(external_id).startswith("github_repo:"):
                found = self.session.scalar(select(IntelItem).where(IntelItem.external_id == external_id))
                if found is not None:
                    return found

        canonical_url = fields.get("canonical_url")
        if canonical_url and fields.get("content_class") == "project_tool":
            found = self.session.scalar(
                select(IntelItem).where(
                    IntelItem.content_class == "project_tool",
                    IntelItem.canonical_url == canonical_url,
                )
            )
            if found is not None:
                return found

        content_hash = fields.get("content_hash")
        if content_hash:
            return self.session.scalar(select(IntelItem).where(IntelItem.content_hash == content_hash))
        return None

    def list_pending_items(
        self,
        *,
        limit: int | None = 100,
        content_class: str | None = None,
        source_id: str | None = None,
        force: bool = False,
    ) -> list[IntelItem]:
        stmt = (
            select(IntelItem)
            .options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review), joinedload(IntelItem.verification))
            .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc(), IntelItem.id.asc())
        )
        if not force:
            stmt = stmt.where(
                IntelItem.status.in_(["new", "selected", "ai_failed"])
                | (
                    (IntelItem.status == "hotspot")
                    & (IntelItem.ai_review.has(AIItemReview.status == "ai_failed"))
                )
            )
        if content_class:
            stmt = stmt.where(IntelItem.content_class == content_class)
        if source_id:
            stmt = stmt.where(IntelItem.source_id == source_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).unique().all())

    def save_selection(self, item_id: int, *, keep: bool, score: int, reason: str) -> IntelItem | None:
        item = self.session.get(IntelItem, item_id)
        if item is None:
            return None
        item.selection_score = max(0, min(int(score), 100))
        item.selection_reason = reason[:4000] if reason else None
        item.status = "selected" if keep else "filtered"
        item.updated_at = utcnow()
        return item

    def save_discovered_links(self, item_id: int, links: list[dict[str, str]]) -> None:
        item = self.session.get(IntelItem, item_id)
        if item is not None:
            item.discovered_links_json = _dump_json(links)
            item.updated_at = utcnow()

    def upsert_ai_review(
        self,
        item_id: int,
        response: Any,
        *,
        model: str | None,
        content_class: str | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> AIItemReview:
        review = self.session.scalar(select(AIItemReview).where(AIItemReview.item_id == item_id))
        if review is None:
            review = AIItemReview(item_id=item_id, content_class=content_class or "community_social")
            self.session.add(review)
        review.model = model
        review.keep = bool(getattr(response, "keep", False)) if response is not None else False
        review.content_class = str(getattr(response, "content_class", None) or content_class or "community_social")
        review.summary_cn = _text(getattr(response, "summary_cn", None))
        review.reason = _text(getattr(response, "reason", None))
        review.risk_flags_json = _dump_json(getattr(response, "risk_flags", []))
        review.needs_verification = bool(getattr(response, "needs_verification", False))
        review.official_url = _text(getattr(response, "official_url", None))
        review.confidence = max(0, min(int(getattr(response, "confidence", 0) or 0), 100))
        raw = getattr(response, "raw_response", None) if response is not None else None
        review.raw_response_json = _dump_json(raw if raw is not None else {})
        review.status = status
        review.error_message = error_message[:4000] if error_message else None
        review.updated_at = utcnow()
        self.session.flush()
        return review

    def upsert_verification(self, item_id: int, result: Any) -> IntelItemVerification:
        verification = self.session.scalar(
            select(IntelItemVerification).where(IntelItemVerification.item_id == item_id)
        )
        if verification is None:
            verification = IntelItemVerification(item_id=item_id, mode="discovery_only", status="skipped")
            self.session.add(verification)
        values = _object_mapping(result)
        for name in (
            "mode", "status", "verification_url", "source_domain", "http_status", "title",
            "content_preview", "supports_basic_fact", "reason",
        ):
            if name in values:
                setattr(verification, name, values[name])
        verification.risk_flags_json = _dump_json(values.get("risk_flags", []))
        verification.checked_at = values.get("checked_at") or utcnow()
        self.session.flush()
        return verification

    def set_item_status(self, item_id: int, status: str) -> None:
        item = self.session.get(IntelItem, item_id)
        if item is not None:
            item.status = status
            item.updated_at = utcnow()

    def list_export_items(
        self,
        *,
        limit: int | None = 100,
        content_class: str | None = None,
        source_id: str | None = None,
    ) -> list[IntelItem]:
        retained_without_ai = (IntelItem.content_class == "project_tool") & (IntelItem.status == "hotspot")
        retained_with_ai = AIItemReview.keep.is_(True) & IntelItem.status.in_(
            ["verified", "hotspot", "discovery_only"]
        )
        stmt = (
            select(IntelItem)
            .options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review), joinedload(IntelItem.verification))
            .outerjoin(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .where(retained_without_ai | retained_with_ai)
            .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc(), IntelItem.id.asc())
        )
        if content_class:
            stmt = stmt.where(IntelItem.content_class == content_class)
        if source_id:
            stmt = stmt.where(IntelItem.source_id == source_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).unique().all())

    def count_by_status(self) -> dict[str, int]:
        rows = self.session.execute(
            select(IntelItem.status).order_by(IntelItem.status)
        ).all()
        counts: dict[str, int] = {}
        for (status,) in rows:
            counts[status] = counts.get(status, 0) + 1
        return counts


def _item_fields(item: Any) -> dict[str, Any]:
    values = _object_mapping(item)
    source_id = _text(values.get("source_id")) or "unknown"
    external_id = _text(values.get("external_id"))
    canonical_url = _canonical_url(values.get("canonical_url") or values.get("link") or values.get("url"))
    title = _text(values.get("title")) or "(untitled)"
    summary = _text(values.get("summary") or values.get("raw_summary"))
    content_text = _text(values.get("content_text") or values.get("content") or values.get("raw_content") or summary)
    content_class = _text(values.get("content_class"))
    if content_class == "project_tool":
        github_url = _canonical_github_url(canonical_url)
        if github_url and (
            (external_id or "").casefold().startswith("github_repo:")
            or github_url.casefold().startswith("https://github.com/")
        ):
            canonical_url = github_url
    metrics = values.get("metrics") or {}
    raw_payload = values.get("raw_payload") or values.get("raw_payload_json") or {}
    if isinstance(metrics, str):
        metrics = _load_json(metrics, {})
    if isinstance(raw_payload, str):
        raw_payload = _load_json(raw_payload, {})
    if not isinstance(metrics, dict):
        metrics = {}
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    published_at = _as_utc(values.get("published_at"))
    captured_at = _as_utc(values.get("captured_at")) or utcnow()
    content_hash = _text(values.get("content_hash")) or _identity_hash(external_id, canonical_url, title, content_text)
    # Keep GitHub repository identity stable as star/description metrics change.
    if external_id and external_id.startswith("github_repo:"):
        content_hash = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    return {
        "source_id": source_id,
        "external_id": external_id,
        "canonical_url": canonical_url,
        "title": title,
        "summary": summary,
        "content_text": content_text,
        "published_at": published_at,
        "captured_at": captured_at,
        "content_class": content_class,
        "metrics": metrics,
        "raw_payload": raw_payload,
        "content_hash": content_hash,
    }


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _policy_dict(policy: Any, kind: str) -> dict[str, Any]:
    if isinstance(policy, Mapping):
        value = policy.get(f"{kind}_policy", {})
        return dict(value) if isinstance(value, Mapping) else {}
    value = getattr(policy, f"{kind}_policy", None)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(exclude_none=True)
        if isinstance(dumped, Mapping):
            return dict(dumped)
    if hasattr(policy, "to_dict"):
        data = policy.to_dict()
        value = data.get(f"{kind}_policy", {})
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def _counts_dict(counts: IntelCounts | Mapping[str, int] | None) -> dict[str, int]:
    if counts is None:
        return {}
    if isinstance(counts, Mapping):
        return {str(k): int(v) for k, v in counts.items()}
    return {key: int(value) for key, value in asdict(counts).items()}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _load_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _is_github_repository_fields(fields: Mapping[str, Any]) -> bool:
    external_id = _text(fields.get("external_id")) or ""
    return external_id.casefold().startswith("github_repo:") and fields.get("content_class") == "project_tool"


def _merge_github_project_metrics(
    previous: Any,
    current: Any,
    *,
    source_id: str,
) -> dict[str, Any]:
    """Merge current GitHub signals without losing other source periods."""

    merged = dict(previous) if isinstance(previous, Mapping) else {}
    values = dict(current) if isinstance(current, Mapping) else {}
    period = _text(values.get("trending_period"))
    if period:
        trending = dict(merged.get("trending")) if isinstance(merged.get("trending"), Mapping) else {}
        previous_period = trending.get(period) if isinstance(trending.get(period), Mapping) else {}
        period_stars = values.get("stars")
        period_forks = values.get("forks")
        if _github_metric_number(previous_period.get("stars")) is not None and (
            _github_metric_number(period_stars) or 0.0
        ) < (_github_metric_number(previous_period.get("stars")) or 0.0):
            period_stars = previous_period.get("stars")
        if _github_metric_number(previous_period.get("forks")) is not None and (
            _github_metric_number(period_forks) or 0.0
        ) < (_github_metric_number(previous_period.get("forks")) or 0.0):
            period_forks = previous_period.get("forks")
        trending[period] = {
            "rank": values.get("trending_rank") if values.get("trending_rank") is not None else previous_period.get("rank"),
            "stars_since": values.get("stars_since") if values.get("stars_since") is not None else previous_period.get("stars_since"),
            "stars": period_stars if period_stars is not None else previous_period.get("stars"),
            "forks": period_forks if period_forks is not None else previous_period.get("forks"),
        }
        merged["trending"] = trending
        # Keep the strongest period signal at the canonical top level too. The
        # selection policy reads this fast path, while ``trending`` preserves
        # daily/weekly history within the current record. A later daily refresh
        # therefore cannot erase a stronger weekly signal.
        best_period = max(
            trending.items(),
            key=lambda entry: (_github_metric_number(entry[1].get("stars_since")) or 0.0, entry[0]),
        )[0]
        merged["trending_period"] = best_period
        merged["trending_rank"] = trending[best_period].get("rank")
        merged["stars_since"] = trending[best_period].get("stars_since")
        merged["trending_signal"] = "stars_since"

    for key, value in values.items():
        if key in {"trending_period", "trending_rank", "stars_since", "trending_signal"}:
            continue
        if key in {"stars", "forks", "watchers", "open_issues"}:
            previous_number = _github_metric_number(merged.get(key))
            current_number = _github_metric_number(value)
            if current_number is not None and (previous_number is None or current_number >= previous_number):
                merged[key] = value
            elif previous_number is None and value is not None:
                merged[key] = value
            continue
        if key == "topics":
            merged[key] = _unique_strings([*_string_values(merged.get(key)), *_string_values(value)])
            continue
        if value is not None:
            merged[key] = value

    merged["discovery_sources"] = _unique_strings(
        [
            *_string_values(merged.get("discovery_sources")),
            *_string_values(values.get("discovery_sources")),
            source_id,
        ]
    )
    query_values = [
        merged.get("search_query"),
        merged.get("query"),
        values.get("search_query"),
        values.get("query"),
    ]
    queries = _unique_strings(query_values)
    if queries:
        merged["search_queries"] = queries
    if source_id.startswith("github_search_topic_"):
        topic = source_id.removeprefix("github_search_topic_")
        merged["search_topics"] = _unique_strings([*_string_values(merged.get("search_topics")), topic])
    query_topics: list[str] = []
    for query in queries:
        query_topics.extend(re.findall(r"(?:^|\s)topic:([^\s]+)", query, flags=re.IGNORECASE))
    if query_topics:
        merged["search_topics"] = _unique_strings([*_string_values(merged.get("search_topics")), *query_topics])
    return merged


def _github_metadata_metrics(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Select bounded, displayable repository fields from GitHub metadata."""

    license_value = metadata.get("license")
    if isinstance(license_value, Mapping):
        license_value = license_value.get("spdx_id") or license_value.get("name")
    topics = metadata.get("topics")
    return {
        key: value
        for key, value in {
            "stars": metadata.get("stargazers_count"),
            "forks": metadata.get("forks_count"),
            "watchers": metadata.get("watchers_count"),
            "open_issues": metadata.get("open_issues_count"),
            "language": metadata.get("language"),
            "topics": list(topics)[:100] if isinstance(topics, list) else None,
            "description": (_text(metadata.get("description")) or "")[:4_000] or None,
            "full_name": _text(metadata.get("full_name")),
            "canonical_project_key": _text(metadata.get("full_name")),
            "pushed_at": metadata.get("pushed_at"),
            "updated_at": metadata.get("updated_at"),
            "created_at": metadata.get("created_at"),
            "archived": metadata.get("archived"),
            "fork": metadata.get("fork"),
            "license": license_value,
            "default_branch": metadata.get("default_branch"),
        }.items()
        if value is not None
    }


def _bounded_github_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only display/enrichment fields and cap free-form description text."""

    allowed = {
        "full_name", "html_url", "description", "topics", "stargazers_count",
        "forks_count", "watchers_count", "open_issues_count", "language",
        "pushed_at", "updated_at", "created_at", "archived", "fork", "license",
        "default_branch", "readme_url",
    }
    bounded = {key: metadata[key] for key in allowed if key in metadata}
    if "description" in bounded:
        bounded["description"] = (_text(bounded.get("description")) or "")[:4_000] or None
    if isinstance(bounded.get("topics"), list):
        bounded["topics"] = [str(topic)[:128] for topic in bounded["topics"][:100] if str(topic).strip()]
    return bounded


def _merge_github_raw_payload(previous: Any, current: Any) -> dict[str, Any]:
    merged = dict(previous) if isinstance(previous, Mapping) else {}
    if isinstance(current, Mapping):
        merged.update(dict(current))
    return merged


def _canonical_github_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (parsed.hostname or "").casefold() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not owner or not repo:
        return None
    return f"https://github.com/{owner.casefold()}/{repo.casefold()}"


def _github_metric_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return result


def _string_values(value: Any) -> list[Any]:
    """Normalize legacy scalar/list metadata before merging it."""

    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _identity_hash(external_id: str | None, url: str | None, title: str, content: str | None) -> str:
    identity = external_id or url or f"{title.lower()}\n{content or ''}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _canonical_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return text
    host = parsed.hostname.lower() if parsed.hostname else parsed.netloc.lower()
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        return text
    if port is not None and not (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", parsed.query))
