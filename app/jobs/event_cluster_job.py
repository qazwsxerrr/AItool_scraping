"""Stage C event aggregation with bounded history and source provenance."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.event_resolution import resolve_event_group
from app.ai.skills.intel_triage import normalize_url
from app.config.limits import DEFAULT_AI_REVIEW_LIMIT
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventItem, IntelEventRankingSnapshot, IntelItem, IntelRunItem
from app.storage.repository import IntelRepository

LOGGER = logging.getLogger(__name__)
_TRACKING_QUERY_KEYS = {"ref", "source", "src", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid"}
_STOPWORDS = {"a", "an", "and", "for", "from", "new", "the", "to", "of", "in", "on", "with", "发布", "推出", "上线", "更新", "官方", "ai", "model", "release", "update"}


@dataclass
class EventClusterResult:
    processed: int = 0
    events: int = 0
    merged: int = 0
    repeats: int = 0
    ambiguous: int = 0
    ai_resolved: int = 0
    ai_failed: int = 0
    snapshots: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)
    snapshot_key: str = "latest"

    @property
    def new_event_ids(self) -> list[int]:
        return self.event_ids


ClusterResult = EventClusterResult


@dataclass(frozen=True)
class _GroupResolution:
    groups: tuple[tuple[dict[str, Any], ...], ...]
    method: str
    confidence: int
    raw: Any = None
    risk_flags: tuple[str, ...] = ()


def normalize_event_title(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_event_url(value: Any) -> str | None:
    if value is None:
        return None
    try:
        raw = normalize_url(value) or str(value).strip()
    except Exception:
        raw = str(value).strip()
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
    query_items = [(key, query_value) for key, query_value in parse_qsl(parts.query, keep_blank_values=True) if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS]
    query_items.sort(key=lambda pair: (pair[0], pair[1]))
    return urlunsplit((scheme, netloc, path, urlencode(query_items, doseq=True), ""))


def _normalize_external_id(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", "", str(value).strip()).casefold()
    return text or None


def exact_identity_keys(value: Any) -> tuple[str, ...]:
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
    keys = exact_identity_keys(value)
    return keys[0] if keys else "title:unknown"


def _title_tokens(value: Any) -> frozenset[str]:
    normalized = normalize_event_title(value)
    return frozenset(token for token in re.findall(r"[\w\u4e00-\u9fff]+", normalized) if token not in _STOPWORDS and len(token) > 1)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def cluster_candidates(candidates: Iterable[Any], *, title_threshold: float = 0.45, fuzzy: bool = True) -> list[list[Any]]:
    rows = [_candidate(value) for value in candidates]
    if not rows:
        return []
    parent = list(range(len(rows)))
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
    for index, row in enumerate(rows):
        for identity in row["identity_keys"]:
            if identity in owners:
                union(owners[identity], index, "exact")
            else:
                owners[identity] = index
    if fuzzy:
        for left in range(len(rows)):
            for right in range(left + 1, len(rows)):
                if find(left) == find(right) or not _compatible_candidate(rows[left], rows[right]):
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
    return {"item": value, "identity_keys": exact_identity_keys(values), "title": str(title), "title_tokens": _title_tokens(title), "topic": _text(values.get("topic")), "content_class": _text(values.get("content_class"))}


def _compatible_candidate(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_topic, right_topic = left.get("topic"), right.get("topic")
    if left_topic and right_topic and left_topic != right_topic:
        return False
    left_class, right_class = left.get("content_class"), right.get("content_class")
    return not (left_class and right_class and left_class != right_class)


def run_event_cluster_job(*, session_factory: sessionmaker[Session], ai_client: Any | None = None, limit: int | None = DEFAULT_AI_REVIEW_LIMIT, force: bool = False, now: datetime | None = None, history_hook: Callable[..., Any] | None = None, history_provider: Callable[..., Any] | None = None, item_ids: Iterable[int] | None = None, snapshot_key: str = "latest", run_id: int | None = None, **_: Any) -> EventClusterResult:
    """Aggregate current Stage B candidates into new or historical events."""
    del force
    result = EventClusterResult(snapshot_key=snapshot_key)
    current = _as_utc(now) or datetime.now(timezone.utc)
    del history_hook, history_provider
    with session_factory() as session:
        try:
            items = _load_cluster_items(session, run_id=run_id, item_ids=item_ids, limit=limit)
            candidates = [_item_candidate(item) for item in items]
            result.processed = len(candidates)
            history_events = _load_history_events(session, current=current, snapshot_key=snapshot_key)
            in_run_events: dict[int, IntelEvent] = {}
            for values, fuzzy_group in _cluster_rows(candidates):
                try:
                    if fuzzy_group:
                        result.ambiguous += 1
                    resolution = _resolve_ambiguous_group(values, ai_client=ai_client, ambiguous=fuzzy_group)
                    if resolution.method.startswith("ai"):
                        result.ai_resolved += 1
                    if "resolver_failed" in resolution.risk_flags:
                        result.ai_failed += 1
                    for subgroup in resolution.groups:
                        event, is_new, _ = _persist_group(session, subgroup, current=current, run_id=run_id, history_events=history_events, in_run_events=in_run_events, resolver=resolution)
                        if is_new:
                            result.events += 1
                            if run_id is None or event.new_in_run_id == run_id:
                                if event.id not in result.event_ids:
                                    result.event_ids.append(event.id)
                        else:
                            result.repeats += 1
                        result.merged += max(0, len(subgroup) - 1)
                        if event.id not in {row.id for row in history_events}:
                            history_events.append(event)
                        if run_id is not None and event.new_in_run_id == run_id:
                            in_run_events[event.id] = event
                except Exception as exc:
                    result.failed += 1
                    result.errors.append(f"event_group={values[0].get('id')}: {exc}")
                    LOGGER.exception("Event aggregation failed for group %s", values[0].get("id"))
                    session.rollback()
            session.commit()
        except Exception as exc:
            session.rollback()
            result.failed += 1
            result.errors.append(str(exc))
            LOGGER.exception("Event cluster job failed")
    return result


def run_cluster_job(**kwargs: Any) -> EventClusterResult:
    return run_event_cluster_job(**kwargs)


def run_event_cluster_from_settings(*, settings: Settings, ai_client: Any | None = None, **kwargs: Any) -> EventClusterResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return run_event_cluster_job(session_factory=create_session_factory(engine), ai_client=ai_client, **kwargs)


def _load_cluster_items(session: Session, *, run_id: int | None, item_ids: Iterable[int] | None, limit: int | None) -> list[IntelItem]:
    stmt = select(IntelItem).options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review), joinedload(IntelItem.ai_screen)).where(IntelItem.status == "candidate").order_by(IntelItem.published_at.desc(), IntelItem.selection_score.desc(), IntelItem.id.asc())
    if item_ids is not None:
        ids = [int(item_id) for item_id in item_ids]
        stmt = stmt.where(IntelItem.id.in_(ids or [-1]))
    elif run_id is not None:
        stmt = stmt.join(IntelRunItem, IntelRunItem.item_id == IntelItem.id).where(IntelRunItem.run_id == int(run_id))
    if limit is not None:
        stmt = stmt.limit(max(0, int(limit)))
    return list(session.scalars(stmt).unique().all())


def _load_history_events(session: Session, *, current: datetime, snapshot_key: str) -> list[IntelEvent]:
    since = current - timedelta(hours=72)
    snapshot_ids = select(IntelEventRankingSnapshot.event_id).where(IntelEventRankingSnapshot.snapshot_key == snapshot_key, IntelEventRankingSnapshot.selected.is_(True))
    stmt = select(IntelEvent).options(joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_review), joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.source), joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source)).where(or_(IntelEvent.last_seen_at >= since, IntelEvent.first_seen_at >= since, IntelEvent.id.in_(snapshot_ids))).order_by(IntelEvent.id.asc())
    return list(session.scalars(stmt).unique().all())


def _item_candidate(item: IntelItem) -> dict[str, Any]:
    review = item.ai_review
    source = item.source
    topics = list(review.topics) if review is not None else []
    if review is not None and review.topic and review.topic not in topics:
        topics.insert(0, review.topic)
    return {
        "id": item.id, "source_id": item.source_id, "source_group": source.source_group if source else None, "source_priority": source.priority if source else 100,
        "content_class": (review.content_class if review else None) or item.content_class, "canonical_url": canonical_event_url(item.canonical_url), "external_id": _normalize_external_id(item.external_id),
        "title": item.title, "normalized_title": normalize_event_title(item.title), "summary_cn": (review.summary_cn if review else None) or item.summary,
        "topic": (review.topic if review else None) or (topics[0] if topics else None), "topics": _clean_strings(topics), "keywords": list(review.keywords) if review is not None else [], "entities": list(review.entities) if review is not None else [], "risk_flags": list(review.risk_flags) if review is not None else [],
        "selection_score": _number(review.selection_score if review else item.selection_score), "published_at": _as_utc(item.published_at or item.discovered_at or item.captured_at), "captured_at": _as_utc(item.captured_at),
        "identity_keys": exact_identity_keys({"canonical_url": item.canonical_url, "external_id": item.external_id, "title": item.title}),
    }


def _cluster_rows(candidates: Sequence[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], bool]]:
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
        fuzzy = any(kinds.get(tuple(sorted((left, right)))) == "fuzzy" for left in range(len(candidates)) for right in range(left + 1, len(candidates)) if find(left) == root and find(right) == root)
        result.append((members, fuzzy))
    return result


def _resolve_ambiguous_group(values: list[dict[str, Any]], *, ai_client: Any | None, ambiguous: bool) -> _GroupResolution:
    if not ambiguous:
        return _GroupResolution((tuple(values),), "deterministic", 100)
    if ai_client is None:
        return _GroupResolution((tuple(values),), "deterministic_fallback", 0, risk_flags=("cluster:ambiguous",))
    resolver = _find_resolver(ai_client)
    if resolver is None:
        return _GroupResolution((tuple(values),), "deterministic_fallback", 0, risk_flags=("resolver_missing",))
    evidence = resolve_event_group(values, resolver)
    if evidence.decision == "merge" and evidence.confidence >= 60:
        return _GroupResolution((tuple(values),), "ai_merge", evidence.confidence, evidence.raw)
    if evidence.decision == "separate":
        return _GroupResolution(tuple((value,) for value in values), "ai_separate", evidence.confidence, evidence.raw)
    return _GroupResolution((tuple(values),), "deterministic_fallback", 0, evidence.raw, ("resolver_failed",))


def resolve_ambiguous_group(values: Iterable[Any], ai_client: Any | None = None) -> list[list[Any]]:
    rows = [_mapping(value) for value in values]
    resolution = _resolve_ambiguous_group(rows, ai_client=ai_client, ambiguous=True)
    return [list(group) for group in resolution.groups]


def _find_resolver(client: Any) -> Callable[..., Any] | None:
    for name in ("resolve_event", "resolve_cluster", "judge_cluster", "cluster", "compare_event"):
        value = getattr(client, name, None)
        if callable(value):
            return value
    return client if callable(client) else None


def _persist_group(session: Session, values: Sequence[Mapping[str, Any]], *, current: datetime, run_id: int | None, history_events: Sequence[IntelEvent], in_run_events: Mapping[int, IntelEvent], resolver: _GroupResolution) -> tuple[IntelEvent, bool, str]:
    projection = _event_projection(values, current=current)
    match, match_kind = _match_history_event(values, history_events, current=current, run_id=run_id, in_run_events=in_run_events)
    if resolver.method == "ai_separate" and match_kind == "repeat_semantic":
        match, match_kind = None, "new"
    is_new = match is None
    if match is not None:
        if float(match.display_score or 0.0) > float(projection.get("display_score") or 0.0):
            projection["title"] = match.title
            projection["summary_cn"] = match.summary_cn
            projection["normalized_title"] = match.normalized_title
            if match.primary_item_id is not None:
                projection["primary_item_id"] = match.primary_item_id
        projection.update(event_key=match.event_key, run_id=run_id, new_in_run_id=match.new_in_run_id, resolution_method=match_kind, resolution_confidence=100 if match_kind == "repeat_exact" else 85)
    else:
        projection.update(run_id=run_id, new_in_run_id=run_id, resolution_method=resolver.method, resolution_confidence=resolver.confidence)
        if resolver.risk_flags:
            projection["risk_flags"] = _clean_strings([*projection.get("risk_flags", []), *resolver.risk_flags])
    repo = IntelRepository(session)
    event = repo.upsert_event(**projection)
    for member in values:
        exact = bool(set(member.get("identity_keys", ())) & set(_json_list(event.identity_keys_json)))
        relation_type = match_kind if match is not None else ("exact" if exact else resolver.method)
        repo.upsert_event_item(event.id, int(member["id"]), source_id=member.get("source_id"), source_group=member.get("source_group"), identity_key=_strongest_identity(member), match_type=relation_type, match_confidence=100 if relation_type in {"exact", "repeat_exact"} else max(0, resolver.confidence), is_primary=int(member["id"]) == int(projection["primary_item_id"]), lineage={"run_id": run_id, "provenance": "new" if is_new else "repeat", "match_type": relation_type, "source_id": member.get("source_id"), "source_group": member.get("source_group"), "canonical_url": member.get("canonical_url"), "external_id": member.get("external_id"), "title": member.get("title")})
    session.flush()
    return event, is_new, match_kind


def _match_history_event(values: Sequence[Mapping[str, Any]], history_events: Sequence[IntelEvent], *, current: datetime, run_id: int | None, in_run_events: Mapping[int, IntelEvent]) -> tuple[IntelEvent | None, str]:
    candidate_ids = {identity for value in values for identity in value.get("identity_keys", ())}
    item_ids = {int(value["id"]) for value in values if value.get("id") is not None}
    seen: set[int] = set()
    for event in [*in_run_events.values(), *history_events]:
        if event.id in seen:
            continue
        seen.add(event.id)
        if any(int(relation.item_id) in item_ids for relation in event.event_items) or candidate_ids & set(_json_list(event.identity_keys_json)):
            return event, "repeat_exact" if (run_id is None or event.new_in_run_id != run_id) else "same_run"
    primary = max(values, key=lambda value: (_number(value.get("selection_score")), _as_utc(value.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)))
    primary_tokens = _title_tokens(primary.get("title"))
    topic, content_class = _text(primary.get("topic")), _text(primary.get("content_class"))
    candidates: list[tuple[float, IntelEvent]] = []
    for event in [*in_run_events.values(), *history_events]:
        event_time = _as_utc(event.last_seen_at or event.first_seen_at)
        if event_time is None or event_time < current - timedelta(hours=72):
            continue
        if topic and event.topic and topic.casefold() != str(event.topic).casefold():
            continue
        if content_class and event.content_class and content_class != event.content_class:
            continue
        similarity = _jaccard(primary_tokens, _title_tokens(event.title))
        if similarity >= 0.70:
            candidates.append((similarity, event))
    if not candidates:
        return None, "new"
    candidates.sort(key=lambda pair: (-pair[0], pair[1].id))
    return candidates[0][1], "repeat_semantic"


def _event_projection(values: Sequence[Mapping[str, Any]], *, current: datetime) -> dict[str, Any]:
    primary = sorted(values, key=lambda value: (-_number(value.get("selection_score")), -((_as_utc(value.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()), int(value.get("source_priority") or 100), int(value.get("id") or 0)))[0]
    times = [_as_utc(value.get("published_at") or value.get("captured_at")) for value in values]
    times = [value for value in times if value is not None]
    return {
        "event_key": canonical_event_key(primary), "canonical_url": primary.get("canonical_url"), "external_id": primary.get("external_id"), "normalized_title": normalize_event_title(primary.get("title")), "title": primary.get("title") or "(untitled)", "summary_cn": primary.get("summary_cn") or primary.get("title"), "topic": primary.get("topic") or "unknown", "topics": _clean_strings(topic for value in values for topic in [value.get("topic"), *value.get("topics", [])]), "keywords": _unique_json_strings(value.get("keywords") for value in values), "entities": _unique_json_objects(entity for value in values for entity in value.get("entities", [])), "content_class": primary.get("content_class"), "source_group": primary.get("source_group"), "source_ids": _clean_strings(value.get("source_id") for value in values), "source_groups": _clean_strings(value.get("source_group") for value in values), "identity_keys": _clean_strings(identity for value in values for identity in value.get("identity_keys", ())), "display_score": max((_number(value.get("selection_score")) for value in values), default=0.0), "risk_flags": _clean_strings(flag for value in values for flag in value.get("risk_flags", [])), "primary_item_id": int(primary["id"]), "first_seen_at": min(times) if times else current, "last_seen_at": max(times) if times else current,
    }


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if isinstance(parsed, str):
        parsed = [parsed]
    return [str(item) for item in parsed if item is not None and str(item).strip()] if isinstance(parsed, list) else []


def _unique_json_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        iterable = [value] if isinstance(value, str) or not isinstance(value, Iterable) else value
        for item in iterable:
            text = str(item).strip() if item is not None else ""
            if text and text not in result:
                result.append(text)
    return result


def _unique_json_objects(values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        row = dict(value)
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


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


def _strongest_identity(value: Mapping[str, Any]) -> str | None:
    return next(iter(value.get("identity_keys", ())), None)


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


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> float:
    try:
        return max(0.0, float(str(value).replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _as_utc(value: Any) -> datetime | None:
    if value is None or not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


__all__ = ["ClusterResult", "EventClusterResult", "build_candidate_clusters", "canonical_event_key", "canonical_event_url", "cluster_candidates", "exact_identity_keys", "normalize_event_title", "resolve_ambiguous_group", "run_cluster_job", "run_event_cluster_from_settings", "run_event_cluster_job"]
