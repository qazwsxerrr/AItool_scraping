"""Stage C event aggregation with bounded history and source provenance."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.event_resolution import event_resolution_client_from_settings, resolve_event_group
from app.ai.skills.intel_triage import normalize_url
from app.config.limits import DEFAULT_AI_REVIEW_LIMIT, RECENT_WINDOW_HOURS
from app.domain.models import COMMUNITY_SOCIAL
from app.domain.policies import is_first_party_x_source
from app.domain.recency import recent_window_decision
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEvent, IntelEventItem, IntelEventStageDSnapshot, IntelItem, IntelRun, IntelRunItem
from app.storage.repository import IntelRepository

LOGGER = logging.getLogger(__name__)
_TRACKING_QUERY_KEYS = {"ref", "source", "src", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid"}
_STOPWORDS = {"a", "an", "and", "for", "from", "new", "the", "to", "of", "in", "on", "with", "发布", "推出", "上线", "更新", "官方", "ai", "model", "release", "update"}
# Stage C v2: only a high-confidence, multi-signal match is merged without
# review.  Lower candidate matches are an AI ambiguity problem, never an
# implicit transitive merge.
SEMANTIC_REPEAT_THRESHOLD = 0.80
SEMANTIC_AMBIGUITY_THRESHOLD = 0.55
DAILY_HISTORY_DAYS = 3


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
    # ``event_ids`` is kept as the historical new-event projection for API
    # compatibility.  Stage D needs the complete current Stage-C projection,
    # including candidates that were matched to an existing event row.
    current_event_ids: list[int] = field(default_factory=list)
    snapshot_key: str = "latest"
    run_id: int | None = None
    reference_time: datetime | None = None

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


def github_repo_identity(value: Any) -> str | None:
    """Return a stable ``owner/repo`` identity for GitHub repository items.

    The collector's ``github_repo:owner/repo`` external id is authoritative.
    URL parsing is only a fallback, and deliberately uses the first two
    GitHub path components so issue, commit and other repository sub-pages
    remain attached to the same repository identity.
    """

    values = _mapping(value)
    if not values and isinstance(value, str):
        values = {"canonical_url": value}

    external_id = _normalize_external_id(values.get("external_id") or values.get("guid"))
    if external_id and external_id.startswith("github_repo:"):
        parts = [part for part in external_id.split(":", 1)[1].split("/") if part]
        if len(parts) >= 2:
            owner = parts[0].strip()
            repo = parts[1].removesuffix(".git").strip()
            if owner and repo:
                return f"{owner}/{repo}".casefold()

    for name in ("canonical_url", "url", "source_url"):
        raw = values.get(name)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        if "://" not in text and text.casefold().startswith(("github.com/", "www.github.com/")):
            text = f"https://{text}"
        try:
            parsed = urlsplit(text)
        except ValueError:
            continue
        host = (parsed.hostname or "").casefold()
        if host not in {"github.com", "www.github.com"}:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            continue
        owner = parts[0].strip()
        repo = re.sub(r"\.git$", "", parts[1], flags=re.IGNORECASE).strip()
        if owner and repo:
            return f"{owner}/{repo}".casefold()
    return None


def exact_identity_keys(value: Any) -> tuple[str, ...]:
    """Return stable identity anchors only (URL/external id, never title)."""

    values = _mapping(value)
    url = canonical_event_url(values.get("canonical_url") or values.get("url") or values.get("source_url"))
    external_id = _normalize_external_id(values.get("external_id") or values.get("guid"))
    keys: list[str] = []
    if url:
        keys.append(f"url:{url}")
    if external_id:
        keys.append(f"external:{external_id}")
    return tuple(dict.fromkeys(keys))


def _weak_title_key(value: Any) -> str | None:
    values = _mapping(value)
    title = normalize_event_title(values.get("normalized_title") or values.get("title") or values.get("original_title"))
    return f"title:{title}" if title else None


def _strong_identity_keys(value: Any) -> frozenset[str]:
    return frozenset(key for key in exact_identity_keys(value) if key.startswith(("url:", "external:")))


def canonical_event_key(value: Any) -> str:
    keys = exact_identity_keys(value)
    if keys:
        return keys[0]
    values = _mapping(value)
    for name in ("id", "item_id", "primary_item_id"):
        try:
            item_id = int(values.get(name))
        except (TypeError, ValueError, OverflowError):
            continue
        if item_id > 0:
            # A title-only item is a weak identity candidate, not a durable
            # event key.  The primary item ID keeps it distinct until Stage C
            # can establish a real URL/external-id or an explicit AI merge.
            return f"item:{item_id}"
    return _weak_title_key(value) or "item:unknown"


def _text_tokens(value: Any) -> frozenset[str]:
    normalized = normalize_event_title(value)
    words = {token for token in re.findall(r"[a-z0-9_]+", normalized) if token not in _STOPWORDS and len(token) > 1}
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    if len(cjk) >= 2:
        words.update(cjk[index : index + 2] for index in range(len(cjk) - 1))
    elif cjk:
        words.add(cjk)
    return frozenset(words)


def _title_tokens(value: Any) -> frozenset[str]:
    return _text_tokens(value)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _normalized_keyword_terms(values: Any) -> frozenset[str]:
    """Return stable, casefolded keyword labels for conservative matching."""

    if values is None:
        return frozenset()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable) or isinstance(values, Mapping):
        values = [values]
    result: set[str] = set()
    for value in values:
        normalized = normalize_event_title(value)
        if normalized and normalized not in _STOPWORDS:
            result.add(normalized)
    return frozenset(result)


def _typed_entity_keys(values: Any) -> frozenset[str]:
    """Normalize typed entities as ``type:name`` keys for overlap scoring."""

    if values is None:
        return frozenset()
    if isinstance(values, Mapping) or isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        values = [values]
    result: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        entity_type = value.get("type") or value.get("entity_type") or value.get("kind") or value.get("category")
        entity_name = value.get("name") or value.get("text") or value.get("value") or value.get("entity")
        name = normalize_event_title(entity_name)
        if not name:
            continue
        result.add(f"{normalize_event_title(entity_type) if entity_type else ''}:{name}")
    return frozenset(result)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_objects(value: Any) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, Mapping)]


def _semantic_match_components(left: Any, right: Any) -> tuple[float, float, float, float]:
    """Return title, keyword, entity and v2 weighted semantic score.

    The public tuple shape remains stable for callers; summary similarity is
    deliberately included in the combined score rather than being hidden
    behind a title-only shortcut.
    """

    title_score = _jaccard(_title_tokens(_field(left, "title") or _field(left, "normalized_title")), _title_tokens(_field(right, "title") or _field(right, "normalized_title")))
    summary_score = _jaccard(
        _text_tokens(_field(left, "summary_cn") or _field(left, "summary")),
        _text_tokens(_field(right, "summary_cn") or _field(right, "summary")),
    )
    left_keywords = _field(left, "keywords", [])
    right_keywords = _field(right, "keywords", _json_list(_field(right, "keywords_json")))
    keyword_score = _jaccard(_normalized_keyword_terms(left_keywords), _normalized_keyword_terms(right_keywords))
    left_entities = _field(left, "entities", [])
    right_entities = _field(right, "entities", _json_objects(_field(right, "entities_json")))
    entity_score = _jaccard(_typed_entity_keys(left_entities), _typed_entity_keys(right_entities))
    combined_score = 0.40 * title_score + 0.25 * summary_score + 0.20 * entity_score + 0.15 * keyword_score
    return title_score, keyword_score, entity_score, combined_score


def _semantic_match_score(left: Any, right: Any) -> float:
    return _semantic_match_components(left, right)[3]


def cluster_candidates(candidates: Iterable[Any], *, title_threshold: float = SEMANTIC_AMBIGUITY_THRESHOLD, fuzzy: bool = True) -> list[list[Any]]:
    rows = [_candidate(value) for value in candidates]
    if not rows:
        return []
    groups = _safe_groups(rows, fuzzy_threshold=title_threshold if fuzzy else None)
    return [[row["item"] for row in group] for group, _ambiguous in groups]


def build_candidate_clusters(candidates: Iterable[Any], **kwargs: Any) -> list[list[Any]]:
    return cluster_candidates(candidates, **kwargs)


def _candidate(value: Any) -> dict[str, Any]:
    values = _mapping(value)
    title = values.get("title") or values.get("original_title") or ""
    return {"item": value, "identity_keys": exact_identity_keys(values), "github_repo_identity": github_repo_identity(values), "title": str(title), "summary_cn": values.get("summary_cn") or values.get("summary"), "title_tokens": _title_tokens(title), "topic": _text(values.get("topic")), "content_class": _text(values.get("content_class")), "keywords": values.get("keywords", []), "entities": values.get("entities", [])}


def _compatible_candidate(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    # Topic/content class describe presentation, not real-world identity.
    # They must not force two reports about one announcement apart.
    left_repo = left.get("github_repo_identity") or github_repo_identity(left)
    right_repo = right.get("github_repo_identity") or github_repo_identity(right)
    if left_repo and right_repo and left_repo != right_repo:
        return False
    return True


def _safe_groups(
    rows: Sequence[dict[str, Any]],
    *,
    fuzzy_threshold: float | None,
) -> list[tuple[list[dict[str, Any]], bool]]:
    """Build conservative groups without union-find transitive collapse.

    A new fuzzy member must satisfy the threshold against every member of the
    candidate group.  Thus A~B and B~C cannot pull C into A's event when
    A~C is weak.  URL/external identity anchors may still join directly.
    """

    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        anchors = set(row.get("identity_keys", ()))
        exact_indexes = [
            index
            for index, group in enumerate(groups)
            if anchors
            and all(_compatible_candidate(row, member) for member in group)
            and any(anchors & set(member.get("identity_keys", ())) for member in group)
        ]
        if exact_indexes:
            first = exact_indexes[0]
            groups[first].append(row)
            # Strong identity may bridge aliases.  This is intentionally the
            # only transitive operation; fuzzy signals never use it.
            for index in reversed(exact_indexes[1:]):
                groups[first].extend(groups.pop(index))
            continue
        if fuzzy_threshold is None:
            groups.append([row])
            continue
        eligible: list[tuple[float, int]] = []
        for index, group in enumerate(groups):
            if not all(_compatible_candidate(row, member) for member in group):
                continue
            scores = [_semantic_match_score(row, member) for member in group]
            if scores and min(scores) >= fuzzy_threshold:
                eligible.append((sum(scores) / len(scores), index))
        if eligible:
            _score, index = max(eligible, key=lambda value: (value[0], -value[1]))
            groups[index].append(row)
        else:
            groups.append([row])

    result: list[tuple[list[dict[str, Any]], bool]] = []
    for group in groups:
        fuzzy_scores = [
            _semantic_match_score(group[left], group[right])
            for left in range(len(group))
            for right in range(left + 1, len(group))
            if not (set(group[left].get("identity_keys", ())) & set(group[right].get("identity_keys", ())))
        ]
        ambiguous = bool(fuzzy_scores and min(fuzzy_scores) < SEMANTIC_REPEAT_THRESHOLD)
        result.append((group, ambiguous))
    return result


def run_event_cluster_job(
    *,
    session_factory: sessionmaker[Session],
    ai_client: Any | None = None,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    force: bool = False,
    now: datetime | None = None,
    reference_time: datetime | None = None,
    history_hook: Callable[..., Any] | None = None,
    history_provider: Callable[..., Any] | None = None,
    item_ids: Iterable[int] | None = None,
    snapshot_key: str | None = None,
    run_id: int | None = None,
    **_: Any,
) -> EventClusterResult:
    """Aggregate current Stage-B candidates into new or historical events.

    When a run id is supplied, its reference time and candidate membership are
    durable inputs.  A retry therefore never needs to invoke Stage A/B or
    consult the mutable ``IntelItem.status`` projection as orchestration state.
    """

    key = _effective_snapshot_key(snapshot_key, run_id)
    result = EventClusterResult(snapshot_key=key, run_id=run_id)
    del history_hook, history_provider
    stage = None
    stage_task = None
    owner = "event-cluster"
    with session_factory() as session:
        try:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id)) if run_id is not None else None
            frozen_reference = _as_utc(reference_time) or _as_utc(run.reference_time if run else None)
            frozen_reference = frozen_reference or _as_utc(now) or datetime.now(timezone.utc)
            if run_id is not None and run is not None:
                stage = repo.ensure_stage(
                    int(run_id),
                    "cluster",
                    reference_time=frozen_reference,
                    metadata={
                        "snapshot_key": key,
                        "history_mode": "prior_selected_daily",
                        "daily_history_days": DAILY_HISTORY_DAYS,
                        "freshness_window_hours": RECENT_WINDOW_HOURS,
                    },
                )
            current = _as_utc(stage.reference_time if stage is not None else frozen_reference) or frozen_reference
            result.reference_time = current
            items = _load_cluster_items(
                session,
                run_id=run_id,
                item_ids=item_ids,
                limit=limit,
                stage=stage,
                reference_time=current,
            )
            candidates = [_item_candidate(item) for item in items]
            result.processed = len(candidates)
            if stage is not None:
                input_fingerprint = _cluster_input_fingerprint(candidates)
                stage_task = repo.ensure_stage_task(
                    stage,
                    subject_type="run",
                    subject_id=int(run_id),
                    target_run_id=int(run_id),
                    input_fingerprint=input_fingerprint,
                    config_fingerprint="cluster-v2",
                )
                claimed = repo.claim_stage_task(
                    stage,
                    task_id=stage_task.id,
                    owner=owner,
                    force=force,
                    input_fingerprint=input_fingerprint,
                    config_fingerprint="cluster-v2",
                )
                if claimed is None:
                    if repo.task_is_reusable(
                        stage_task,
                        input_fingerprint=input_fingerprint,
                        config_fingerprint="cluster-v2",
                    ):
                        stored = _mapping(stage_task.result)
                        result.event_ids = _event_id_list(stored.get("event_ids"))
                        result.current_event_ids = _event_id_list(
                            stored.get("current_event_ids"),
                            fallback=result.event_ids,
                        )
                        result.processed = 0
                        return result
                    result.failed = 1
                    result.errors.append("cluster stage is already running")
                    return result
                stage_task = claimed
                # Keep the lease/task claim durable independently from event
                # writes.  A failed group must not roll it back.
                session.commit()
            history_events = _load_history_events(
                session,
                current=current,
                snapshot_key=key,
                run=run,
            )
            in_run_events: dict[int, IntelEvent] = {}
            for values, fuzzy_group in _cluster_rows(candidates):
                try:
                    with session.begin_nested():
                        if fuzzy_group:
                            result.ambiguous += 1
                        resolution = _resolve_ambiguous_group(values, ai_client=ai_client, ambiguous=fuzzy_group)
                        if resolution.method.startswith("ai"):
                            result.ai_resolved += 1
                        if "resolver_failed" in resolution.risk_flags:
                            result.ai_failed += 1
                        for subgroup in resolution.groups:
                            event, is_new, _ = _persist_group(
                                session,
                                subgroup,
                                current=current,
                                run_id=run_id,
                                history_events=history_events,
                                in_run_events=in_run_events,
                                resolver=resolution,
                                ai_client=ai_client,
                            )
                            if is_new:
                                result.events += 1
                                if run_id is None or event.new_in_run_id == run_id:
                                    if event.id not in result.event_ids:
                                        result.event_ids.append(event.id)
                            else:
                                result.repeats += 1
                            if event.id not in result.current_event_ids:
                                result.current_event_ids.append(event.id)
                            result.merged += max(0, len(subgroup) - 1)
                            if event.id not in {row.id for row in history_events}:
                                history_events.append(event)
                            if run_id is not None and event.new_in_run_id == run_id:
                                in_run_events[event.id] = event
                except Exception as exc:
                    result.failed += 1
                    result.errors.append(f"event_group={values[0].get('id')}: {exc}")
                    LOGGER.exception("Event aggregation failed for group %s", values[0].get("id"))
                    # The nested transaction above isolates one bad group;
                    # already persisted events and the stage lease survive.
            session.commit()
            if stage_task is not None:
                if result.failed:
                    repo.fail_stage_task(
                        stage_task,
                        error_category="stage",
                        error_code="cluster_group_failed",
                        error_message="; ".join(result.errors)[-4000:],
                        retryable=True,
                        owner=owner,
                    )
                else:
                    repo.complete_stage_task(
                        stage_task,
                        owner=owner,
                        result={
                            "event_ids": result.event_ids,
                            "current_event_ids": result.current_event_ids,
                            "processed": result.processed,
                        },
                    )
                session.commit()
        except Exception as exc:
            session.rollback()
            result.failed += 1
            result.errors.append(str(exc))
            LOGGER.exception("Event cluster job failed")
            if run_id is not None:
                try:
                    with session_factory() as state_session:
                        state_repo = IntelRepository(state_session)
                        state_stage = state_repo.get_stage(int(run_id), "cluster")
                        state_task = state_repo.get_task(state_stage, subject_type="run", subject_id=int(run_id)) if state_stage else None
                        if state_task is not None and state_task.status == "running":
                            state_repo.fail_stage_task(
                                state_task,
                                error_category="stage",
                                error_code="cluster_failed",
                                error_message=str(exc),
                                retryable=True,
                                owner=owner,
                            )
                        state_session.commit()
                except Exception:
                    LOGGER.exception("Unable to persist cluster stage failure")
    return result


def run_cluster_job(**kwargs: Any) -> EventClusterResult:
    return run_event_cluster_job(**kwargs)


def run_event_cluster_from_settings(*, settings: Settings, ai_client: Any | None = None, **kwargs: Any) -> EventClusterResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    resolver = ai_client
    if resolver is None:
        resolver = event_resolution_client_from_settings(settings)
    return run_event_cluster_job(session_factory=create_session_factory(engine), ai_client=resolver, **kwargs)


def _effective_snapshot_key(snapshot_key: str | None, run_id: int | None) -> str:
    if snapshot_key:
        return str(snapshot_key)
    return f"run-{int(run_id)}" if run_id is not None else "latest"


def _cluster_input_fingerprint(candidates: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "id": int(value.get("id")),
            "title": value.get("title"),
            "summary_cn": value.get("summary_cn"),
            "url": value.get("canonical_url"),
            "external_id": value.get("external_id"),
            "topic": value.get("topic"),
            "content_class": value.get("content_class"),
            "keywords": value.get("keywords"),
            "entities": value.get("entities"),
            "published_at": _iso_datetime(value.get("published_at")),
            "score": value.get("selection_score"),
        }
        for value in candidates
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_id_list(value: Any, *, fallback: Iterable[int] = ()) -> list[int]:
    """Normalize persisted Stage-C event id projections without duplicates."""

    values = value if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)) else fallback
    result: list[int] = []
    for item in values:
        try:
            event_id = int(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if event_id > 0 and event_id not in result:
            result.append(event_id)
    return result


def _load_cluster_items(
    session: Session,
    *,
    run_id: int | None,
    item_ids: Iterable[int] | None,
    limit: int | None,
    stage: Any | None = None,
    reference_time: datetime | None = None,
) -> list[IntelItem]:
    stmt = select(IntelItem).options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review), joinedload(IntelItem.ai_screen)).order_by(IntelItem.published_at.desc(), IntelItem.selection_score.desc(), IntelItem.id.asc())
    if item_ids is not None:
        ids = [int(item_id) for item_id in item_ids]
        stmt = stmt.where(IntelItem.id.in_(ids or [-1]))
    elif run_id is not None:
        stmt = stmt.join(IntelRunItem, IntelRunItem.item_id == IntelItem.id).where(IntelRunItem.run_id == int(run_id))
        # Prefer durable Stage-B task state.  The mutable item status remains
        # only a compatibility fallback for old runs with no task table row.
        task_ids = _successful_analysis_item_ids(session, int(run_id)) if stage is not None else []
        if task_ids:
            stmt = stmt.where(IntelItem.id.in_(task_ids))
        else:
            # Older runs may not have durable Stage-B tasks yet.  A successful
            # candidate review tied to this run is still trustworthy and
            # avoids falling back to the mutable global item status.
            review_ids: list[int] = []
            for review in session.scalars(
                select(AIItemReview)
                .join(IntelItem, AIItemReview.item_id == IntelItem.id)
                .where(
                    AIItemReview.run_id == int(run_id),
                    AIItemReview.status == "success",
                )
            ).all():
                # ``status=success`` describes a completed provider call.  It
                # does not mean that Stage B kept the item as a candidate:
                # filtered analyses are intentionally persisted as successful
                # projections for auditability.  Keep the compatibility path
                # aligned with the durable-task path below.
                if not _analysis_result_is_filtered({"reason": review.reason}):
                    review_ids.append(int(review.item_id))
            stmt = stmt.where(IntelItem.id.in_(review_ids or [-1]))
    else:
        stmt = stmt.where(IntelItem.status == "candidate")
    items = list(session.scalars(stmt).unique().all())
    if run_id is not None and reference_time is not None:
        items = [
            item
            for item in items
            if recent_window_decision(
                item,
                source=item.source,
                reference_time=reference_time,
            ).eligible
        ]
    if limit is not None:
        return items[: max(0, int(limit))]
    return items


def _successful_analysis_item_ids(session: Session, run_id: int) -> list[int]:
    """Read successful Stage-B *candidate* tasks for ``run_id``.

    Stage B records both retained candidates and ``analysis_filtered`` items
    with task status ``succeeded`` because the provider call itself completed
    successfully.  Stage C must therefore inspect the task result (and the
    run-scoped review as a compatibility fallback), rather than treating task
    success as candidate eligibility.
    """

    from app.storage.models import IntelRunStage, IntelRunStageTask

    stage = session.scalar(
        select(IntelRunStage).where(
            IntelRunStage.run_id == int(run_id),
            IntelRunStage.stage_name.in_(("analyze", "analysis", "stage_b", "stage-b")),
        )
    )
    if stage is None:
        return []
    review_reasons = {
        int(item_id): reason
        for item_id, reason in session.execute(
            select(AIItemReview.item_id, AIItemReview.reason).where(
                AIItemReview.run_id == int(run_id),
            )
        ).all()
    }
    values: list[int] = []
    for task in session.scalars(
        select(IntelRunStageTask).where(
            IntelRunStageTask.stage_id == stage.id,
            IntelRunStageTask.subject_type == "item",
            IntelRunStageTask.status == "succeeded",
        )
    ).all():
        item_id: int | None = None
        if task.item_id is not None:
            item_id = int(task.item_id)
        elif str(task.subject_id).isdigit():
            item_id = int(task.subject_id)
        if item_id is None:
            continue

        # Current Stage B tasks persist the normalized AnalysisResult, whose
        # reason is prefixed with ``analysis_filtered:`` for rejected output.
        # Older rows may only have the run-scoped AIItemReview projection, so
        # consult that projection when the task result has no marker.
        if _analysis_result_is_filtered(task.result):
            continue
        if _analysis_result_is_filtered({"reason": review_reasons.get(item_id)}):
            continue
        values.append(item_id)
    return values


def _analysis_result_is_filtered(value: Any) -> bool:
    """Return whether a Stage-B result is an analysis-filtered projection."""

    data = _mapping(value)
    reason = _text(data.get("reason"))
    if reason is not None and reason.casefold().startswith("analysis_filtered"):
        return True
    if data.get("filtered") is True:
        return True
    metadata = _mapping(data.get("metadata"))
    return metadata.get("filtered") is True


def _load_history_events(
    session: Session,
    *,
    current: datetime,
    snapshot_key: str,
    run: IntelRun | None = None,
    daily_history_days: int = DAILY_HISTORY_DAYS,
) -> list[IntelEvent]:
    """Load only previous public daily selections for a run-scoped edition.

    The database remains the audit store, but the current daily event pool is
    not deduplicated against every recent event row.  For normal pipeline
    runs, historical matching is deliberately limited to the latest public
    edition for each recent date.  The run-less branch preserves the legacy
    compatibility facade used by older direct callers.
    """

    if run is not None:
        return _load_selected_daily_history_events(
            session,
            run=run,
            days=daily_history_days,
        )

    # Legacy direct calls have no public edition boundary. Keep their former
    # bounded behaviour instead of inventing a date-addressed daily scope.
    since = current - timedelta(hours=72)
    snapshot_ids = select(IntelEventStageDSnapshot.event_id).where(IntelEventStageDSnapshot.snapshot_key == snapshot_key, IntelEventStageDSnapshot.selected.is_(True))
    stmt = select(IntelEvent).options(joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_review), joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.source), joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source)).where(or_(IntelEvent.last_seen_at >= since, IntelEvent.first_seen_at >= since, IntelEvent.id.in_(snapshot_ids))).order_by(IntelEvent.id.asc())
    return list(session.scalars(stmt).unique().all())


def _load_selected_daily_history_events(
    session: Session,
    *,
    run: IntelRun,
    days: int,
) -> list[IntelEvent]:
    if days <= 0 or not run.edition_date:
        return []
    try:
        current_edition = date.fromisoformat(run.edition_date)
    except ValueError:
        return []

    earliest = current_edition - timedelta(days=days)
    previous_runs = list(
        session.scalars(
            select(IntelRun)
            .where(
                IntelRun.status.in_(("completed", "completed_with_errors", "partial")),
                IntelRun._edition_date >= earliest,
                IntelRun._edition_date < current_edition,
            )
            .order_by(IntelRun._edition_date.desc(), IntelRun.id.desc())
        ).all()
    )
    # A public date can have several internal attempts. Only its newest run
    # represents the final daily output that subsequent editions compare to.
    latest_by_edition: dict[str, IntelRun] = {}
    for previous_run in previous_runs:
        if previous_run.edition_date:
            latest_by_edition.setdefault(previous_run.edition_date, previous_run)
    conditions = [
        and_(
            IntelEventStageDSnapshot.run_id == int(previous_run.id),
            IntelEventStageDSnapshot.snapshot_key == previous_run.daily_snapshot_key,
        )
        for previous_run in latest_by_edition.values()
    ]
    event_ids: list[int] = []
    if conditions:
        event_ids.extend(
            int(event_id)
            for event_id in session.scalars(
                select(IntelEventStageDSnapshot.event_id).where(
                    IntelEventStageDSnapshot.selected.is_(True),
                    or_(*conditions),
                )
            ).all()
        )
    # A Stage-C retry must also see events already materialized by this same
    # run. This is execution-local idempotency, not cross-day historical
    # recall, and prevents a retry from creating a second canonical event.
    event_ids.extend(
        int(event_id)
        for event_id in session.scalars(
            select(IntelEvent.id).where(
                or_(
                    IntelEvent.first_run_id == int(run.id),
                    IntelEvent.last_run_id == int(run.id),
                    IntelEvent.new_in_run_id == int(run.id),
                )
            )
        ).all()
    )
    event_ids = list(dict.fromkeys(event_ids))
    if not event_ids:
        return []
    stmt = (
        select(IntelEvent)
        .options(
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_review),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.source),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source),
        )
        .where(IntelEvent.id.in_(event_ids))
        .order_by(IntelEvent.id.asc())
    )
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
        "github_repo_identity": github_repo_identity(item),
        "title": item.title, "normalized_title": normalize_event_title(item.title), "summary_cn": (review.summary_cn if review else None) or item.summary,
        "topic": (review.topic if review else None) or (topics[0] if topics else None), "topics": _clean_strings(topics), "keywords": list(review.keywords) if review is not None else [], "entities": list(review.entities) if review is not None else [], "risk_flags": list(review.risk_flags) if review is not None else [],
        "selection_score": _number(review.selection_score if review else item.selection_score), "published_at": _as_utc(item.published_at or item.discovered_at or item.captured_at), "captured_at": _as_utc(item.captured_at),
        "identity_keys": exact_identity_keys({"canonical_url": item.canonical_url, "external_id": item.external_id, "title": item.title}),
    }


def _cluster_rows(candidates: Sequence[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], bool]]:
    return _safe_groups(list(candidates), fuzzy_threshold=SEMANTIC_AMBIGUITY_THRESHOLD)


def _resolve_ambiguous_group(values: list[dict[str, Any]], *, ai_client: Any | None, ambiguous: bool) -> _GroupResolution:
    github_repos = {
        repo
        for value in values
        for repo in [value.get("github_repo_identity") or github_repo_identity(value)]
        if repo
    }
    if len(github_repos) > 1:
        # A provider must never be allowed to merge two known repository
        # identities, even if the semantic group was ambiguous and the model
        # claims ``merge``.  Keep each item auditable as a guarded singleton.
        return _GroupResolution(
            tuple((value,) for value in values),
            "deterministic_fallback",
            0,
            risk_flags=("github_repo_mismatch",),
        )
    if not ambiguous:
        return _GroupResolution((tuple(values),), "deterministic", 100)
    if ai_client is None:
        return _GroupResolution(tuple((value,) for value in values), "deterministic_fallback", 0, risk_flags=("ambiguous_unresolved",))
    resolver = _find_resolver(ai_client)
    if resolver is None:
        return _GroupResolution(tuple((value,) for value in values), "deterministic_fallback", 0, risk_flags=("resolver_missing", "ambiguous_unresolved"))
    evidence = resolve_event_group(values, resolver)
    if evidence.decision == "merge" and evidence.confidence >= 80:
        return _GroupResolution((tuple(values),), "ai_merge", evidence.confidence, evidence.raw)
    if evidence.decision == "partition" and evidence.confidence >= 80:
        groups = _resolution_partition(values, evidence.groups)
        if groups is not None:
            return _GroupResolution(groups, "ai_partition", evidence.confidence, evidence.raw)
    if evidence.decision == "separate":
        return _GroupResolution(tuple((value,) for value in values), "ai_separate", evidence.confidence, evidence.raw)
    return _GroupResolution(tuple((value,) for value in values), "deterministic_fallback", 0, evidence.raw, ("resolver_failed", "ambiguous_unresolved"))


def _resolution_partition(
    values: Sequence[Mapping[str, Any]],
    groups: Sequence[Sequence[int]],
) -> tuple[tuple[dict[str, Any], ...], ...] | None:
    """Accept only a complete, disjoint partition of the supplied item ids."""

    by_id = {int(value["id"]): dict(value) for value in values if value.get("id") is not None}
    expected = set(by_id)
    assigned: set[int] = set()
    resolved: list[tuple[dict[str, Any], ...]] = []
    for raw_group in groups:
        ids: list[int] = []
        for raw_id in raw_group:
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError, OverflowError):
                return None
            if item_id not in by_id:
                return None
            ids.append(item_id)
        if not ids or len(ids) != len(set(ids)) or any(item_id in assigned for item_id in ids):
            return None
        assigned.update(ids)
        resolved.append(tuple(by_id[item_id] for item_id in ids))
    return tuple(resolved) if assigned == expected and resolved else None


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


def _persist_group(session: Session, values: Sequence[Mapping[str, Any]], *, current: datetime, run_id: int | None, history_events: Sequence[IntelEvent], in_run_events: Mapping[int, IntelEvent], resolver: _GroupResolution, ai_client: Any | None) -> tuple[IntelEvent, bool, str]:
    projection = _event_projection(values, current=current)
    group_resolution_method = resolver.method
    match, match_kind = _match_history_event(values, history_events, current=current, run_id=run_id, in_run_events=in_run_events)
    if match is not None and match_kind == "ambiguous_semantic":
        match, match_kind, resolver = _resolve_history_ambiguity(values, match, resolver=resolver, ai_client=ai_client)
    if group_resolution_method in {"ai_separate", "ai_partition", "deterministic_fallback"} and match_kind in {"repeat_semantic", "repeat_semantic_ai"}:
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
    if resolver.raw is not None:
        projection["resolution_raw"] = resolver.raw
    repo = IntelRepository(session)
    event = repo.upsert_event(**projection)
    for member in values:
        exact = bool(_strong_identity_keys(member) & _strong_identity_keys({"canonical_url": event.canonical_url, "external_id": event.external_id}))
        relation_type = match_kind if match is not None else ("exact_url_or_external" if exact else resolver.method)
        repo.upsert_event_item(event.id, int(member["id"]), source_id=member.get("source_id"), source_group=member.get("source_group"), identity_key=_strongest_identity(member), match_type=relation_type, match_confidence=100 if relation_type in {"exact_url_or_external", "repeat_exact"} else max(0, resolver.confidence), is_primary=int(member["id"]) == int(projection["primary_item_id"]), lineage={"run_id": run_id, "provenance": "new" if is_new else "repeat", "match_type": relation_type, "source_id": member.get("source_id"), "source_group": member.get("source_group"), "canonical_url": member.get("canonical_url"), "external_id": member.get("external_id"), "title": member.get("title")})
    _reconcile_event_social_only_risk(event)
    session.flush()
    return event, is_new, match_kind


def _reconcile_event_social_only_risk(event: IntelEvent) -> None:
    """Keep the event-level social-only flag aligned with its full lineage.

    A historical official-X row can carry the former community classification.
    The strict source identity triple remains authoritative, so an event with
    any first-party or other non-community member is not a social-only event.
    Individual source/item audits retain their original risk flags.
    """

    members = list(event.event_items)
    if not members:
        return
    community_members = [member for member in members if _event_member_is_community(member)]
    trusted_members = [member for member in members if not _event_member_is_community(member)]
    flags = [flag for flag in _json_list(event.risk_flags_json) if flag != "source:social_only"]
    if community_members and not trusted_members:
        flags.append("source:social_only")
    event.risk_flags_json = json.dumps(_clean_strings(flags), ensure_ascii=False)


def _event_member_is_community(member: IntelEventItem) -> bool:
    item = member.item
    source = member.source or (item.source if item is not None else None)
    # The configured account identity is stronger than stale stored classes
    # from before the first-party X policy was introduced.
    if source is not None and is_first_party_x_source(source):
        return False
    review = item.ai_review if item is not None else None
    content_class = _text(
        (review.content_class if review is not None else None)
        or (item.content_class if item is not None else None)
        or (source.content_class if source is not None else None)
    )
    review_flags = set(review.risk_flags if review is not None else [])
    return content_class == COMMUNITY_SOCIAL or "source:social_only" in review_flags


def _resolve_history_ambiguity(values: Sequence[Mapping[str, Any]], event: IntelEvent, *, resolver: _GroupResolution, ai_client: Any | None) -> tuple[IntelEvent | None, str, _GroupResolution]:
    """Resolve a sub-threshold history candidate without inventing event copy."""

    if ai_client is None:
        return None, "new_ambiguous", _GroupResolution(resolver.groups, "deterministic_fallback", 0, risk_flags=("history:ambiguous",))
    client_resolver = _find_resolver(ai_client)
    if client_resolver is None:
        return None, "new_ambiguous", _GroupResolution(resolver.groups, "deterministic_fallback", 0, risk_flags=("history:resolver_missing",))
    evidence = resolve_event_group([*values, _event_resolution_value(event)], client_resolver)
    if evidence.decision == "merge" and evidence.confidence >= 80:
        return event, "repeat_semantic_ai", _GroupResolution(resolver.groups, "ai_merge", evidence.confidence, evidence.raw)
    if evidence.decision == "separate":
        return None, "new", _GroupResolution(resolver.groups, "ai_separate", evidence.confidence, evidence.raw)
    return None, "new_ambiguous", _GroupResolution(resolver.groups, "deterministic_fallback", 0, evidence.raw, ("history:resolver_failed",))


def _event_resolution_value(event: IntelEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "canonical_url": event.canonical_url,
        "external_id": event.external_id,
        "title": event.title,
        "summary_cn": event.summary_cn,
        "topic": event.topic,
        "topics": _json_list(event.topics_json),
        "keywords": _json_list(event.keywords_json),
        "entities": _json_objects(event.entities_json),
        "content_class": event.content_class,
        "source_group": event.source_group,
    }


def _event_github_repo_identities(event: IntelEvent) -> frozenset[str]:
    identities: set[str] = set()
    direct = github_repo_identity(event)
    if direct:
        identities.add(direct)
    for relation in getattr(event, "event_items", ()) or ():
        relation_identity = getattr(relation, "identity_key", None)
        if isinstance(relation_identity, str):
            if relation_identity.startswith("external:"):
                relation_repo = github_repo_identity({"external_id": relation_identity[9:]})
            elif relation_identity.startswith("url:"):
                relation_repo = github_repo_identity({"canonical_url": relation_identity[4:]})
            else:
                relation_repo = None
            if relation_repo:
                identities.add(relation_repo)
        item = getattr(relation, "item", None)
        identity = github_repo_identity(item) if item is not None else None
        if identity:
            identities.add(identity)
    return frozenset(identities)


def _event_strong_identity_keys(event: IntelEvent) -> frozenset[str]:
    identities = set(_strong_identity_keys(event))
    for relation in getattr(event, "event_items", ()) or ():
        relation_identity = getattr(relation, "identity_key", None)
        if isinstance(relation_identity, str) and relation_identity.startswith(("url:", "external:")):
            identities.add(relation_identity)
        item = getattr(relation, "item", None)
        if item is not None:
            identities.update(_strong_identity_keys(item))
    return frozenset(identities)


def _history_github_repo_compatible(candidate_repos: frozenset[str], event_repos: frozenset[str]) -> bool:
    """Allow semantic history only for a single compatible repo identity."""

    if len(candidate_repos) > 1 or len(event_repos) > 1:
        return False
    if candidate_repos and event_repos and candidate_repos != event_repos:
        return False
    return True


def _match_history_event(values: Sequence[Mapping[str, Any]], history_events: Sequence[IntelEvent], *, current: datetime, run_id: int | None, in_run_events: Mapping[int, IntelEvent]) -> tuple[IntelEvent | None, str]:
    candidate_ids = {identity for value in values for identity in _strong_identity_keys(value)}
    item_ids = {int(value["id"]) for value in values if value.get("id") is not None}
    candidate_repos = frozenset(
        repo
        for value in values
        for repo in [value.get("github_repo_identity") or github_repo_identity(value)]
        if repo
    )
    seen: set[int] = set()
    for event in [*in_run_events.values(), *history_events]:
        if event.id in seen:
            continue
        seen.add(event.id)
        event_identity = _event_strong_identity_keys(event)
        if any(int(relation.item_id) in item_ids for relation in event.event_items) or candidate_ids & event_identity:
            return event, "repeat_exact" if (run_id is None or event.new_in_run_id != run_id) else "same_run"
    primary = max(values, key=lambda value: (_number(value.get("selection_score")), _as_utc(value.get("published_at")) or datetime.min.replace(tzinfo=timezone.utc)))
    candidates: list[tuple[float, IntelEvent]] = []
    for event in [*in_run_events.values(), *history_events]:
        event_time = _as_utc(event.last_seen_at or event.first_seen_at)
        if event_time is None or event_time < current - timedelta(hours=72):
            continue
        if not _history_github_repo_compatible(candidate_repos, _event_github_repo_identities(event)):
            continue
        similarity = _semantic_match_score(primary, event)
        if similarity >= SEMANTIC_AMBIGUITY_THRESHOLD:
            candidates.append((similarity, event))
    if not candidates:
        return None, "new"
    candidates.sort(key=lambda pair: (-pair[0], pair[1].id))
    return candidates[0][1], "repeat_semantic" if candidates[0][0] >= SEMANTIC_REPEAT_THRESHOLD else "ambiguous_semantic"


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
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value is None or not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso_datetime(value: Any) -> str | None:
    current = _as_utc(value)
    return current.isoformat() if current is not None else None


__all__ = ["ClusterResult", "EventClusterResult", "build_candidate_clusters", "canonical_event_key", "canonical_event_url", "cluster_candidates", "exact_identity_keys", "github_repo_identity", "normalize_event_title", "resolve_ambiguous_group", "run_cluster_job", "run_event_cluster_from_settings", "run_event_cluster_job"]
