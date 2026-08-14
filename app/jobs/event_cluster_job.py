"""Deterministic event aggregation for the AI Intel Triage pipeline.

The job intentionally keeps event aggregation independent from editorial
quotas and report copy.  It consumes normalized :class:`IntelItem` rows (and,
when available, Wave 1 ``TriageResult`` values), persists one canonical event
per exact identity, and records every member/source relation for auditability.
Ambiguous title-only groups may be resolved by an injected AI adapter; any
provider failure falls back to the deterministic candidate group and is
recorded on the event instead of aborting the batch.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.skills.intel_triage import TriageResult, normalize_url, parse_triage_result
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelItem
from app.storage.repository import IntelRepository


LOGGER = logging.getLogger(__name__)

_TRACKING_QUERY_KEYS = {
    "ref", "source", "src", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid",
}
_STOPWORDS = {
    "a", "an", "and", "for", "from", "new", "the", "to", "of", "in", "on", "with",
    "发布", "推出", "上线", "更新", "官方", "ai", "model", "release", "update",
}
_DEFAULT_ITEM_STATUSES = (
    "new", "selected", "hotspot", "rejected", "verified", "discovery_only", "ai_failed",
)


@dataclass
class EventClusterResult:
    processed: int = 0
    events: int = 0
    merged: int = 0
    ambiguous: int = 0
    ai_resolved: int = 0
    ai_failed: int = 0
    snapshots: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    snapshot_key: str = "latest"


# Historical callers used ``ClusterResult``/``run_cluster_job`` names.
ClusterResult = EventClusterResult


@dataclass(frozen=True)
class _Resolution:
    groups: tuple[tuple[dict[str, Any], ...], ...]
    method: str
    confidence: int
    raw: Any = None
    risk_flags: tuple[str, ...] = ()
    canonical_title: str | None = None
    event_key: str | None = None


def normalize_event_title(value: Any) -> str:
    """Return a stable exact-title identity for event dedupe."""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    text = re.sub(r"\s+", " ", text)
    # Keep Unicode letters/numbers (including Chinese), drop punctuation and
    # feed separators, then collapse whitespace once more.
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_event_url(value: Any) -> str | None:
    """Normalize an event URL, removing tracking-only query parameters."""

    if value is None:
        return None
    try:
        normalized = normalize_url(value)
    except Exception:
        normalized = None
    raw = normalized or str(value).strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.casefold().rstrip("/") or None
    if not parts.scheme or not parts.netloc:
        return raw.casefold().rstrip("/") or None
    scheme = parts.scheme.casefold()
    host = (parts.hostname or parts.netloc).casefold()
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    query_items = [
        (key, query_value)
        for key, query_value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    query_items.sort(key=lambda pair: (pair[0], pair[1]))
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def _normalize_external_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return re.sub(r"\s+", "", text).casefold() or None


def exact_identity_keys(value: Any) -> tuple[str, ...]:
    """Return URL/external-id/title aliases in strongest-first order."""

    values = _mapping(value)
    url = canonical_event_url(values.get("canonical_url") or values.get("url") or values.get("source_url"))
    external_id = _normalize_external_id(values.get("external_id") or values.get("guid"))
    title = normalize_event_title(values.get("normalized_title") or values.get("title") or values.get("original_title"))
    keys: list[str] = []
    if url:
        keys.append(f"url:{url}")
    if external_id:
        keys.append(f"external:{external_id}")
    if title:
        keys.append(f"title:{title}")
    return tuple(dict.fromkeys(keys))


def canonical_event_key(value: Any) -> str:
    """Return a deterministic event key based on exact identity aliases."""

    keys = exact_identity_keys(value)
    if keys:
        return keys[0]
    return "title:unknown"


def _title_tokens(value: Any) -> frozenset[str]:
    normalized = normalize_event_title(value)
    return frozenset(
        token
        for token in re.findall(r"[\w\u4e00-\u9fff]+", normalized)
        if token not in _STOPWORDS and len(token) > 1
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def cluster_candidates(
    candidates: Iterable[Any],
    *,
    title_threshold: float = 0.45,
    fuzzy: bool = True,
) -> list[list[Any]]:
    """Build deterministic candidate clusters.

    Exact URL/external-id/title aliases are unioned first.  Remaining items
    with compatible topic/content class and sufficiently similar titles are
    placed in a candidate group for optional AI resolution.  The function is
    pure and preserves input order within each group.
    """

    rows = [_candidate(value) for value in candidates]
    if not rows:
        return []
    parent = list(range(len(rows)))
    edge_kind: dict[tuple[int, int], str] = {}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int, kind: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left
        edge_kind[tuple(sorted((left, right)))] = kind

    identity_owner: dict[str, int] = {}
    for index, row in enumerate(rows):
        for identity in row["identity_keys"]:
            owner = identity_owner.get(identity)
            if owner is not None:
                union(owner, index, "exact")
            else:
                identity_owner[identity] = index

    if fuzzy:
        for left in range(len(rows)):
            for right in range(left + 1, len(rows)):
                if find(left) == find(right):
                    continue
                if not _compatible_candidate(rows[left], rows[right]):
                    continue
                if _jaccard(rows[left]["title_tokens"], rows[right]["title_tokens"]) >= title_threshold:
                    union(left, right, "fuzzy")

    grouped: dict[int, list[Any]] = {}
    order: list[int] = []
    for index, row in enumerate(rows):
        root = find(index)
        if root not in grouped:
            grouped[root] = []
            order.append(root)
        grouped[root].append(row["item"])
    return [grouped[root] for root in order]


def build_candidate_clusters(candidates: Iterable[Any], **kwargs: Any) -> list[list[Any]]:
    return cluster_candidates(candidates, **kwargs)


def _candidate(value: Any) -> dict[str, Any]:
    values = _mapping(value)
    title = values.get("title") or values.get("original_title") or ""
    topic = str(values.get("topic") or "").strip().casefold() or None
    content_class = str(values.get("content_class") or "").strip().casefold() or None
    identities = exact_identity_keys(values)
    return {
        "item": value,
        "identity_keys": identities,
        "title": str(title),
        "title_tokens": _title_tokens(title),
        "topic": topic,
        "content_class": content_class,
    }


def _compatible_candidate(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_topic, right_topic = left.get("topic"), right.get("topic")
    if left_topic and right_topic and left_topic != right_topic:
        return False
    left_class, right_class = left.get("content_class"), right.get("content_class")
    if left_class and right_class and left_class != right_class:
        return False
    return True


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        try:
            return dict(value.model_dump(mode="python"))
        except TypeError:
            return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def run_event_cluster_job(
    *,
    session_factory: sessionmaker[Session],
    ai_client: Any | None = None,
    limit: int | None = 100,
    force: bool = False,
    now: datetime | None = None,
    history_hook: Callable[..., Any] | None = None,
    history_provider: Callable[..., Any] | None = None,
    triage_results: Mapping[Any, Any] | Iterable[Any] | None = None,
    item_ids: Iterable[int] | None = None,
    snapshot_key: str = "latest",
    run_id: int | None = None,
) -> EventClusterResult:
    """Aggregate candidate items into events and persist a ranking snapshot."""

    result = EventClusterResult(snapshot_key=snapshot_key)
    current = _as_utc(now) or datetime.now(timezone.utc)
    history = history_hook or history_provider
    triage_map = _triage_mapping(triage_results)
    with session_factory() as session:
        try:
            items = _load_cluster_items(session, limit=limit, force=force, item_ids=item_ids)
            candidates = [_item_candidate(item, triage_map.get(item.id)) for item in items]
            groups = _cluster_rows(candidates)
            resolved_groups: list[tuple[list[dict[str, Any]], _Resolution]] = []
            for values, fuzzy_group in groups:
                if not values:
                    continue
                result.processed += len(values)
                if fuzzy_group:
                    result.ambiguous += 1
                resolution = _resolve_ambiguous_group(values, ai_client=ai_client, ambiguous=fuzzy_group)
                if resolution.method.startswith("ai"):
                    result.ai_resolved += 1
                if "ai_failed" in resolution.risk_flags:
                    result.ai_failed += 1
                for subgroup in resolution.groups:
                    resolved_groups.append((list(subgroup), resolution))

            persisted_events: list[IntelEvent] = []
            for values, resolution in resolved_groups:
                try:
                    event_values = _event_values(values, current=current, history_hook=history, resolution=resolution)
                    repo = IntelRepository(session)
                    existing_event = repo.find_event_for_item(int(event_values["primary_item_id"]))
                    event = repo.upsert_event(
                        event_key=event_values["event_key"],
                        canonical_url=event_values.get("canonical_url"),
                        external_id=event_values.get("external_id"),
                        normalized_title=event_values.get("normalized_title"),
                        title=event_values.get("title"),
                        summary_cn=event_values.get("summary_cn"),
                        topic=event_values.get("topic"),
                        topics=event_values.get("topics", []),
                        content_class=event_values.get("content_class"),
                        source_group=event_values.get("source_group"),
                        source_ids=event_values.get("source_ids", []),
                        source_groups=event_values.get("source_groups", []),
                        identity_keys=event_values.get("identity_keys", []),
                        display_score=event_values.get("display_score", 0.0),
                        novelty_status=event_values.get("novelty_status", "unknown"),
                        state="candidate",
                        resolution_method=event_values.get("resolution_method", resolution.method),
                        resolution_confidence=event_values.get("resolution_confidence", resolution.confidence),
                        resolution_raw=event_values.get("resolution_raw"),
                        risk_flags=event_values.get("risk_flags", []),
                        primary_item_id=event_values.get("primary_item_id"),
                        first_seen_at=event_values.get("first_seen_at"),
                        last_seen_at=event_values.get("last_seen_at"),
                    )
                    for index, member in enumerate(values):
                        member_match = "exact" if _member_has_exact_identity(member, values) else resolution.method
                        repo.upsert_event_item(
                            event.id,
                            int(member["id"]),
                            source_id=member.get("source_id"),
                            source_group=member.get("source_group"),
                            identity_key=_strongest_identity(member),
                            match_type=member_match,
                            match_confidence=100 if member_match == "exact" else resolution.confidence,
                            is_primary=int(member["id"]) == int(event_values["primary_item_id"]),
                            lineage={
                                "source_id": member.get("source_id"),
                                "source_group": member.get("source_group"),
                                "canonical_url": member.get("canonical_url"),
                                "external_id": member.get("external_id"),
                                "title": member.get("title"),
                                "match_type": member_match,
                                "triage_status": member.get("triage_status"),
                            },
                        )
                    persisted_events.append(event)
                    if event.id not in result.event_ids:
                        result.event_ids.append(event.id)
                    result.events += int(existing_event is None)
                    result.merged += max(0, len(values) - 1)
                except Exception as exc:
                    result.failed += 1
                    message = f"event_group={values[0].get('id')}: {exc}"
                    result.errors.append(message)
                    LOGGER.exception("Event aggregation failed for group %s", values[0].get("id"))
                    session.rollback()

            # Event-level ranking is deliberately based on the aggregated
            # display score only; raw source selection scores are never
            # compared as cross-source feed rankings in later stages.
            unique_events = {event.id: event for event in persisted_events}
            ordered = sorted(
                unique_events.values(),
                key=lambda event: (-float(event.display_score or 0.0), event.event_key, event.id),
            )
            repo = IntelRepository(session)
            for rank, event in enumerate(ordered, start=1):
                snapshot = repo.upsert_event_ranking_snapshot(
                    event.id,
                    snapshot_key=snapshot_key,
                    run_id=run_id,
                    rank=rank,
                    display_score=float(event.display_score or 0.0),
                    selected=bool(_event_selected(event)),
                    topic=event.topic,
                    source_group=event.source_group,
                    content_class=event.content_class,
                    reason=event.resolution_method,
                    metadata={"event_key": event.event_key, "novelty_status": event.novelty_status},
                )
                result.snapshots += int(snapshot.created)
            session.commit()
        except Exception as exc:
            session.rollback()
            result.failed += 1
            result.errors.append(str(exc))
            LOGGER.exception("Event cluster job failed")
    return result


def run_cluster_job(**kwargs: Any) -> EventClusterResult:
    return run_event_cluster_job(**kwargs)


def run_event_cluster_from_settings(
    *,
    settings: Settings,
    ai_client: Any | None = None,
    **kwargs: Any,
) -> EventClusterResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    client = ai_client
    if client is None:
        # ItemAnalysisClient intentionally has no event resolver; passing None
        # preserves the deterministic fallback while keeping this entry point
        # useful for scheduled jobs that inject a resolver later.
        client = None
    return run_event_cluster_job(
        session_factory=create_session_factory(engine),
        ai_client=client,
        **kwargs,
    )


def _load_cluster_items(
    session: Session,
    *,
    limit: int | None,
    force: bool,
    item_ids: Iterable[int] | None,
) -> list[IntelItem]:
    stmt = (
        select(IntelItem)
        .options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review))
        .order_by(IntelItem.published_at.desc(), IntelItem.selection_score.desc(), IntelItem.id.asc())
    )
    if item_ids is not None:
        ids = [int(item_id) for item_id in item_ids]
        stmt = stmt.where(IntelItem.id.in_(ids or [-1]))
    elif not force:
        stmt = stmt.where(IntelItem.status.in_(_DEFAULT_ITEM_STATUSES))
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).unique().all())


def _triage_mapping(values: Mapping[Any, Any] | Iterable[Any] | None) -> dict[int, Any]:
    if values is None:
        return {}
    if isinstance(values, Mapping):
        result: dict[int, Any] = {}
        for key, value in values.items():
            try:
                result[int(key)] = value
            except (TypeError, ValueError):
                continue
        return result
    result = {}
    for value in values:
        item_id = _mapping(value).get("item_id")
        if item_id is None:
            continue
        try:
            result[int(item_id)] = value
        except (TypeError, ValueError):
            continue
    return result


def _item_candidate(item: IntelItem, triage: Any | None) -> dict[str, Any]:
    triage_values = _coerce_triage_values(triage)
    review_values: dict[str, Any] = {}
    review = item.ai_review
    has_persisted_triage = False
    if review is not None:
        raw_review = _load_json(review.raw_response_json, {})
        if isinstance(raw_review, Mapping):
            # ``raw_response_json`` is retained for audit, but explicit columns
            # below take precedence whenever the row came from Wave 1 triage.
            review_values = dict(raw_review)
        persisted_topics = _load_json(getattr(review, "topics_json", "[]"), [])
        persisted_keywords = _load_json(getattr(review, "keywords_json", "[]"), [])
        persisted_scores = _load_json(getattr(review, "scores_json", "{}"), {})
        persisted_paper = _load_json(getattr(review, "paper_support_json", "{}"), {})
        has_persisted_triage = bool(
            getattr(review, "topic", None)
            or persisted_topics
            or persisted_keywords
            or persisted_scores
            or persisted_paper
            or getattr(review, "novelty", None)
        )
        explicit_review = {
            "topic": getattr(review, "topic", None),
            "topics": persisted_topics,
            "keywords": persisted_keywords,
            "selection_score": getattr(review, "selection_score", None),
            "scores": persisted_scores,
            "novelty": getattr(review, "novelty", None),
            "novelty_score": getattr(review, "novelty_score", 0),
            "paper_support": persisted_paper,
            "summary_cn": review.summary_cn,
            "risk_flags": _load_json(review.risk_flags_json, []),
            "content_class": review.content_class,
            "keep": review.keep,
            "confidence": review.confidence,
            "status": review.status,
        }
        # Only non-empty triage projections override raw legacy values.  This
        # keeps historical ItemAnalysis rows readable without inventing topic
        # or novelty values for them.
        review_values.update(
            {
                key: value
                for key, value in explicit_review.items()
                if value is not None
                and (
                    key not in {"topics", "keywords", "scores", "paper_support", "risk_flags"}
                    or bool(value)
                )
            }
        )
        if not has_persisted_triage:
            # A legacy raw payload may contain provider-specific keys that
            # happen to look like triage.  Do not let those keys bypass the
            # explicit projection requirement.
            for key in (
                "topic", "topics", "keywords", "selection_score", "scores",
                "novelty", "novelty_status", "novelty_score", "paper_support",
            ):
                review_values.pop(key, None)
    merged = {**review_values, **triage_values}
    raw_item = _load_json(item.raw_payload_json, {})
    metrics = _load_json(item.metrics_json, {})
    source = item.source
    topic = str(merged.get("topic") or "").strip().casefold() or None
    score = _number(merged.get("selection_score", merged.get("display_score", item.selection_score)))
    summary = _text(merged.get("summary_cn") or item.summary)
    published = _as_utc(item.published_at or item.discovered_at or item.captured_at)
    identities = exact_identity_keys(
        {
            "canonical_url": canonical_event_url(item.canonical_url),
            "external_id": item.external_id,
            "title": item.title,
        }
    )
    return {
        "id": item.id,
        "item": item,
        "source_id": item.source_id,
        "source_name": source.name if source else None,
        "source_group": source.source_group if source else None,
        "source_priority": source.priority if source else 100,
        "content_class": str(merged.get("content_class") or item.content_class or "community_social"),
        "canonical_url": canonical_event_url(item.canonical_url),
        "external_id": _normalize_external_id(item.external_id),
        "title": item.title,
        "normalized_title": normalize_event_title(item.title),
        "summary_cn": summary,
        "topic": topic,
        "topics": _clean_strings(merged.get("topics")),
        "keywords": _clean_strings(merged.get("keywords")),
        "risk_flags": _clean_strings(merged.get("risk_flags")),
        "novelty": _normalize_novelty(merged.get("novelty") or merged.get("novelty_status")),
        "triage_status": _text(merged.get("status")) if (triage_values or has_persisted_triage) else None,
        "keep": bool(merged.get("keep", item.status in {"selected", "hotspot"})),
        "confidence": _number(merged.get("confidence")),
        "selection_score": score,
        "metrics": metrics if isinstance(metrics, Mapping) else {},
        "raw_payload": raw_item if isinstance(raw_item, Mapping) else {},
        "published_at": published,
        "captured_at": _as_utc(item.captured_at),
        "identity_keys": identities,
    }


def _coerce_triage_values(value: Any | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, TriageResult):
        return value.model_dump(mode="python")
    values = _mapping(value)
    if not values:
        return {}
    # A strict Wave 1 result is preferred when all required fields are
    # present, but lightweight fakes/mappings are retained as-is.
    if "topic" in values and "summary_cn" in values:
        try:
            parsed = parse_triage_result(values)
            return parsed.model_dump(mode="python")
        except Exception:
            return values
    return values


def _cluster_rows(candidates: Sequence[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], bool]]:
    """Return groups plus whether a group contains fuzzy-only links."""

    if not candidates:
        return []
    parent = list(range(len(candidates)))
    kinds: dict[tuple[int, int], str] = {}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int, kind: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root
        kinds[tuple(sorted((left, right)))] = kind

    owners: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        for identity in candidate["identity_keys"]:
            if identity in owners:
                union(owners[identity], index, "exact")
            else:
                owners[identity] = index
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if find(left) == find(right) or not _compatible_candidate(candidates[left], candidates[right]):
                continue
            if _jaccard(_title_tokens(candidates[left]["title"]), _title_tokens(candidates[right]["title"])) >= 0.45:
                union(left, right, "fuzzy")

    groups: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for index, candidate in enumerate(candidates):
        root = find(index)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(candidate)
    result: list[tuple[list[dict[str, Any]], bool]] = []
    for root in order:
        members = groups[root]
        fuzzy_group = False
        for left_index, left in enumerate(candidates):
            if find(left_index) != root:
                continue
            for right_index in range(left_index + 1, len(candidates)):
                if find(right_index) == root and kinds.get(tuple(sorted((left_index, right_index)))) == "fuzzy":
                    fuzzy_group = True
        result.append((members, fuzzy_group))
    return result


def _resolve_ambiguous_group(
    values: list[dict[str, Any]],
    *,
    ai_client: Any | None,
    ambiguous: bool,
) -> _Resolution:
    if not ambiguous:
        return _Resolution((tuple(values),), "deterministic", 100)
    if ai_client is None:
        return _Resolution((tuple(values),), "deterministic_fallback", 0, risk_flags=("cluster:ambiguous",))

    resolver = _find_resolver(ai_client)
    if resolver is None:
        return _Resolution((tuple(values),), "deterministic_fallback", 0, risk_flags=("cluster:resolver_missing",))

    # Prefer a group-level resolver; pairwise fallbacks cover historical fake
    # clients exposing ``judge_cluster(left, right)``.
    try:
        raw = resolver(values)
        parsed = _parse_resolution(raw, values)
        if parsed is not None:
            return parsed
    except TypeError:
        pass
    except Exception as exc:
        return _Resolution((tuple(values),), "deterministic_fallback", 0, raw={"error": str(exc)}, risk_flags=("cluster:ai_failed",))

    accepted: list[tuple[int, int]] = []
    raw_results: list[Any] = []
    for left_index in range(1, len(values)):
        left, right = values[0], values[left_index]
        try:
            raw = resolver(left, right)
            raw_results.append(raw)
            if _accept_resolution(raw):
                accepted.append((0, left_index))
        except Exception as exc:
            raw_results.append({"error": str(exc)})
            continue
    if not raw_results:
        return _Resolution((tuple(values),), "deterministic_fallback", 0, risk_flags=("cluster:ai_failed",))
    if accepted:
        return _Resolution((tuple(values),), "ai_merge", 80, raw=raw_results)
    # A valid pairwise separate judgement is stronger than an unavailable
    # resolver; keep members separate so a false merge cannot leak downstream.
    if any(_is_separate_resolution(raw) for raw in raw_results):
        return _Resolution(tuple((value,) for value in values), "ai_separate", 80, raw=raw_results)
    return _Resolution((tuple(values),), "deterministic_fallback", 0, raw=raw_results, risk_flags=("cluster:ai_failed",))


def resolve_ambiguous_group(values: Iterable[Any], ai_client: Any | None = None) -> list[list[Any]]:
    rows = [_mapping(value) for value in values]
    resolution = _resolve_ambiguous_group(rows, ai_client=ai_client, ambiguous=True)
    return [list(group) for group in resolution.groups]


def _find_resolver(client: Any) -> Callable[..., Any] | None:
    for name in ("resolve_event", "resolve_cluster", "judge_cluster", "cluster", "compare_event"):
        value = getattr(client, name, None)
        if callable(value):
            return value
    return None


def _parse_resolution(raw: Any, values: list[dict[str, Any]]) -> _Resolution | None:
    data = _mapping(raw)
    if not data and isinstance(raw, str):
        data = {"decision": raw}
    groups_value = data.get("groups") or data.get("clusters")
    if isinstance(groups_value, (list, tuple)) and groups_value:
        by_id = {str(value["id"]): value for value in values}
        groups: list[tuple[dict[str, Any], ...]] = []
        used: set[str] = set()
        for group in groups_value:
            if not isinstance(group, (list, tuple)):
                continue
            members: list[dict[str, Any]] = []
            for token in group:
                key = str(_mapping(token).get("id", token))
                if key in by_id:
                    members.append(by_id[key])
                    used.add(key)
            if members:
                groups.append(tuple(members))
        for value in values:
            if str(value["id"]) not in used:
                groups.append((value,))
        if groups:
            return _Resolution(
                tuple(groups),
                "ai_split" if len(groups) > 1 else "ai_merge",
                _confidence(data, 80),
                raw=raw,
                canonical_title=_text(data.get("canonical_title") or data.get("title")),
                event_key=_text(data.get("event_key") or data.get("canonical_event_key")),
            )
    decision = _decision_text(data or raw)
    if decision in {"merge", "related"} and _confidence(data, 80) >= 60:
        return _Resolution(
            (tuple(values),),
            "ai_merge",
            _confidence(data, 80),
            raw=raw,
            canonical_title=_text(data.get("canonical_title") or data.get("event_title") or data.get("title")),
            event_key=_text(data.get("event_key") or data.get("canonical_event_key")),
        )
    if decision in {"separate", "split", "unrelated"}:
        return _Resolution(tuple((value,) for value in values), "ai_separate", _confidence(data, 80), raw=raw)
    if isinstance(raw, bool):
        return _Resolution((tuple(values),), "ai_merge" if raw else "ai_separate", 80, raw=raw) if raw else _Resolution(tuple((value,) for value in values), "ai_separate", 80, raw=raw)
    return None


def _accept_resolution(raw: Any) -> bool:
    data = _mapping(raw)
    if isinstance(raw, bool):
        return raw
    return _decision_text(data or raw) in {"merge", "related"} and _confidence(data, 80) >= 60


def _is_separate_resolution(raw: Any) -> bool:
    return _decision_text(_mapping(raw) or raw) in {"separate", "split", "unrelated"}


def _decision_text(value: Any) -> str | None:
    data = _mapping(value)
    decision = data.get("decision") or data.get("resolution") or data.get("relation") or data.get("merge")
    if isinstance(decision, bool):
        return "merge" if decision else "separate"
    return str(decision).strip().casefold() if decision is not None else None


def _confidence(data: Mapping[str, Any], default: int) -> int:
    value = data.get("confidence", data.get("score", default))
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def _event_values(
    values: list[dict[str, Any]],
    *,
    current: datetime,
    history_hook: Callable[..., Any] | None,
    resolution: _Resolution,
) -> dict[str, Any]:
    primary = sorted(
        values,
        key=lambda value: (
            -_number(value.get("selection_score")),
            -(_as_utc(value.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            int(value.get("source_priority") or 100),
            int(value.get("id") or 0),
        ),
    )[0]
    aliases = _clean_strings(identity for value in values for identity in value.get("identity_keys", ()))
    key = resolution.event_key or canonical_event_key(primary)
    if key == "title:unknown" and aliases:
        key = aliases[0]
    title = resolution.canonical_title or primary.get("title") or "(untitled)"
    topics = _clean_strings(topic for value in values for topic in [value.get("topic"), *value.get("topics", [])] if topic)
    novelty = _resolve_novelty(values, history_hook=history_hook, current=current)
    risk_flags = _clean_strings(flag for value in values for flag in value.get("risk_flags", []))
    if history_hook is not None and novelty == "unknown" and "history:missing" not in risk_flags:
        risk_flags.append("history:missing")
    if resolution.risk_flags:
        risk_flags.extend(flag for flag in resolution.risk_flags if flag not in risk_flags)
    seen_times = [_as_utc(value.get("published_at") or value.get("captured_at")) for value in values]
    seen_times = [value for value in seen_times if value is not None]
    return {
        "event_key": key,
        "canonical_url": primary.get("canonical_url"),
        "external_id": primary.get("external_id"),
        "normalized_title": normalize_event_title(title),
        "title": title,
        "summary_cn": primary.get("summary_cn") or primary.get("title"),
        # ``unknown`` is explicit cold-start state.  A missing triage record
        # must never be silently published as an opinion event.
        "topic": primary.get("topic") or (topics[0] if topics else "unknown"),
        "topics": topics,
        "content_class": primary.get("content_class"),
        "source_group": primary.get("source_group"),
        "source_ids": _clean_strings(value.get("source_id") for value in values),
        "source_groups": _clean_strings(value.get("source_group") for value in values),
        "identity_keys": aliases,
        "display_score": max((_number(value.get("selection_score")) for value in values), default=0.0),
        "novelty_status": novelty,
        "resolution_method": resolution.method,
        "resolution_confidence": resolution.confidence,
        "resolution_raw": resolution.raw,
        "risk_flags": risk_flags,
        "primary_item_id": int(primary["id"]),
        "first_seen_at": min(seen_times) if seen_times else current,
        "last_seen_at": max(seen_times) if seen_times else current,
    }


def _resolve_novelty(values: Sequence[Mapping[str, Any]], *, history_hook: Callable[..., Any] | None, current: datetime) -> str:
    """Use the optional 72-hour history hook without rejecting missing history."""

    if history_hook is None:
        return "unknown"
    since = current - timedelta(hours=72)
    try:
        history = history_hook(values, since=since, now=current)
    except TypeError:
        try:
            history = history_hook(values, since)
        except TypeError:
            history = history_hook(values)
    except Exception:
        return "unknown"
    if history is None or history is False or history == [] or history == {}:
        return "unknown"
    if isinstance(history, Mapping):
        value = history.get("novelty_status") or history.get("novelty") or history.get("status")
        if value is None and history.get("exists") is True:
            value = "repeat"
    elif isinstance(history, bool):
        value = "repeat" if history else "unknown"
    else:
        value = history
    normalized = _normalize_novelty(value)
    return normalized or "unknown"


def _normalize_novelty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold().replace("-", "_")
    aliases = {"new_item": "new", "novel": "new", "fresh": "new", "updated": "update", "duplicate": "repeat", "old": "repeat", "undetermined": "unknown"}
    text = aliases.get(text, text)
    return text if text in {"new", "update", "repeat", "unknown"} else "unknown"


def _member_has_exact_identity(member: Mapping[str, Any], values: Sequence[Mapping[str, Any]]) -> bool:
    identities = set(member.get("identity_keys", ()))
    return any(identities & set(other.get("identity_keys", ())) for other in values if other is not member)


def _strongest_identity(value: Mapping[str, Any]) -> str | None:
    identities = value.get("identity_keys", ())
    return next(iter(identities), None)


def _event_selected(event: IntelEvent) -> bool:
    # The event stage does not enforce editorial quotas; selected means at
    # least one contributing item was retained by the prior stage.
    return any(relation.item.status in {"selected", "hotspot"} for relation in event.event_items if relation.item is not None)


def _clean_strings(values: Iterable[Any] | Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[,，;；|]+", values)
    elif not isinstance(values, Iterable):
        values = [values]
    result: list[str] = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in result:
            result.append(text)
    return result


def _number(value: Any) -> float:
    try:
        return max(0.0, float(str(value).replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_utc(value: Any) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _load_json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return parsed if parsed is not None else default


__all__ = [
    "ClusterResult",
    "EventClusterResult",
    "build_candidate_clusters",
    "canonical_event_key",
    "canonical_event_url",
    "cluster_candidates",
    "exact_identity_keys",
    "normalize_event_title",
    "resolve_ambiguous_group",
    "run_cluster_job",
    "run_event_cluster_from_settings",
    "run_event_cluster_job",
]
