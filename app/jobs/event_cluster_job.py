"""Stage C: successful Stage-B projections become aggregated stories in one AI call."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.skills.intel_triage import normalize_url
from app.ai.skills.stage_c_aggregation import (
    STAGE_C_PROMPT_VERSION,
    STAGE_C_SCHEMA_VERSION,
    StageCAggregationCallResult,
    StageCAggregationClient,
    StageCStoryCluster,
    strict_parse_stage_c_aggregation,
)
from app.config.limits import (
    DEFAULT_AI_STAGE_C_INPUT_MIN_SCORE,
    STAGE_C_INPUT_POLICY_VERSION,
)
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    DailyEdition,
    DailyEditionReportEntry,
    IntelEvent,
    IntelItem,
    IntelRun,
    IntelRunItem,
    IntelRunStage,
    IntelRunStageTask,
)
from app.storage.repository import IntelRepository


DAILY_HISTORY_DAYS = 3
_TRACKING_QUERY_KEYS = {"ref", "source", "src", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid"}


@dataclass
class EventClusterResult:
    run_id: int
    processed: int = 0
    events: int = 0
    merged: int = 0
    repeats: int = 0
    updated: int = 0
    event_ids: list[int] = field(default_factory=list)
    current_event_ids: list[int] = field(default_factory=list)
    reference_time: datetime | None = None
    input_audit: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _StageCInputSelection:
    items: list[IntelItem]
    audit: dict[str, Any]


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
    query_items = [
        (key, query_value)
        for key, query_value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
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
    keys = [value for value in (f"url:{url}" if url else None, f"external:{external_id}" if external_id else None) if value]
    return tuple(dict.fromkeys(keys))


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
            return f"item:{item_id}"
    title = normalize_event_title(values.get("title") or values.get("original_title"))
    return f"title:{title}" if title else "item:unknown"


def run_event_cluster_job(
    *,
    session_factory: sessionmaker[Session],
    run_id: int,
    ai_client: Any,
    force: bool = False,
    now: datetime | None = None,
    reference_time: datetime | None = None,
    input_min_score: int = DEFAULT_AI_STAGE_C_INPUT_MIN_SCORE,
) -> EventClusterResult:
    """Aggregate the current Stage-B item set in one AI request."""

    result = EventClusterResult(run_id=int(run_id))
    owner = "stage-c-ai-aggregation"
    raw_response: Any | None = None
    request_metadata: Mapping[str, Any] | None = None
    min_score = _bounded_score(input_min_score, DEFAULT_AI_STAGE_C_INPUT_MIN_SCORE)
    model_name = str(getattr(ai_client, "model", None) or "custom")
    config_fingerprint = _stage_c_config_fingerprint(
        model_name=model_name,
        input_min_score=min_score,
    )

    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None or run.edition_id is None:
            raise ValueError("Stage C requires the current daily edition build")
        frozen_reference = _as_utc(reference_time) or _as_utc(run.reference_time) or _as_utc(now)
        frozen_reference = frozen_reference or datetime.now(timezone.utc)
        stage = repo.ensure_stage(
            int(run_id),
            "cluster",
            config_fingerprint=config_fingerprint,
            reference_time=frozen_reference,
            metadata={
                "aggregation_mode": "ai_single_call",
                "history_mode": "prior_published_daily_reports",
                "daily_history_days": DAILY_HISTORY_DAYS,
                "prompt_version": STAGE_C_PROMPT_VERSION,
                "input_policy_version": STAGE_C_INPUT_POLICY_VERSION,
                "input_min_score": min_score,
            },
        )
        current = _as_utc(stage.reference_time) or frozen_reference
        result.reference_time = current
        selection = _load_cluster_items(
            session,
            run_id=int(run_id),
            input_min_score=min_score,
        )
        result.input_audit = selection.audit
        candidates = [_item_candidate(item) for item in selection.items]
        history = _load_published_daily_history(session, run=run, days=DAILY_HISTORY_DAYS)
        result.processed = len(candidates)
        input_fingerprint = _cluster_input_fingerprint(candidates, history)
        task = repo.ensure_stage_task(
            stage,
            subject_type="run",
            subject_id=int(run_id),
            target_run_id=int(run_id),
            input_fingerprint=input_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        claimed = repo.claim_stage_task(
            stage,
            task_id=task.id,
            owner=owner,
            force=force,
            input_fingerprint=input_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        if claimed is None:
            if repo.task_is_reusable(
                task,
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
            ):
                stored = _mapping(task.result)
                result.event_ids = _event_id_list(stored.get("event_ids"))
                result.current_event_ids = _event_id_list(stored.get("current_event_ids"))
                result.input_audit = _mapping(stored.get("input_audit"))
                result.processed = 0
                return result
            raise RuntimeError("Stage C task is already running")
        task = claimed
        session.commit()

        try:
            if not candidates:
                _clear_build_events(session, run_id=int(run_id))
                repo.complete_stage_task(
                    task,
                    owner=owner,
                    result={
                        "schema_version": STAGE_C_SCHEMA_VERSION,
                        "event_ids": [],
                        "current_event_ids": [],
                        "processed": 0,
                        "clusters": 0,
                        "input_audit": result.input_audit,
                    },
                    metadata={"input_audit": result.input_audit},
                )
                session.commit()
                return result

            if ai_client is None or not callable(getattr(ai_client, "aggregate", None)):
                raise TypeError("Stage C requires an AI client with aggregate()")
            call = ai_client.aggregate(
                candidates,
                recent_history=history,
                edition={
                    "edition_date": run.edition_date,
                    "reference_time": current.isoformat(),
                },
            )
            if not isinstance(call, StageCAggregationCallResult):
                raise TypeError("Stage C aggregate() must return StageCAggregationCallResult")
            raw_response = call.raw_response
            request_metadata = call.request_metadata
            parsed = strict_parse_stage_c_aggregation(
                call.parsed.model_dump(mode="json"),
                item_ids=[int(row["id"]) for row in candidates],
                prior_event_keys=[str(row["event_key"]) for row in history],
            )
            _clear_build_events(session, run_id=int(run_id))
            by_id = {int(row["id"]): row for row in candidates}
            event_keys: set[str] = set()
            for cluster in parsed.clusters:
                event_key = cluster.prior_event_key or canonical_event_key(by_id[cluster.primary_item_id])
                if event_key in event_keys:
                    raise ValueError(f"Stage C response produces duplicate event_key: {event_key}")
                event_keys.add(event_key)
                event = _persist_ai_cluster(
                    session,
                    run_id=int(run_id),
                    current=current,
                    cluster=cluster,
                    candidates=by_id,
                    request_metadata=request_metadata,
                )
                result.current_event_ids.append(int(event.id))
                result.merged += max(0, len(cluster.members) - 1)
                if cluster.novelty_status == "new":
                    result.events += 1
                    result.event_ids.append(int(event.id))
                elif cluster.novelty_status == "updated":
                    result.updated += 1
                else:
                    result.repeats += 1

            repo.complete_stage_task(
                task,
                owner=owner,
                result={
                    "schema_version": STAGE_C_SCHEMA_VERSION,
                    "event_ids": result.event_ids,
                    "current_event_ids": result.current_event_ids,
                    "processed": result.processed,
                    "clusters": len(parsed.clusters),
                    "input_audit": result.input_audit,
                    "request_metadata": dict(request_metadata or {}),
                },
                raw_response=raw_response,
                metadata={
                    **dict(request_metadata or {}),
                    "input_audit": result.input_audit,
                },
            )
            session.commit()
            return result
        except Exception as exc:
            session.rollback()
            raw_response = getattr(exc, "raw_response", raw_response)
            request_metadata = getattr(exc, "request_metadata", request_metadata)
            with session_factory() as failure_session:
                failure_repo = IntelRepository(failure_session)
                failure_stage = failure_repo.get_stage(int(run_id), "cluster")
                failure_task = (
                    failure_repo.get_task(
                        failure_stage,
                        subject_type="run",
                        subject_id=int(run_id),
                    )
                    if failure_stage is not None
                    else None
                )
                if failure_task is not None and failure_task.status == "running":
                    failure_repo.fail_stage_task(
                        failure_task,
                        error_category="provider",
                        error_code="stage_c_ai_aggregation_failed",
                        error_message=str(exc),
                        retryable=False,
                        raw_response={
                            "provider_response": raw_response,
                            "request_metadata": dict(request_metadata or {}),
                        },
                        owner=owner,
                    )
                failure_session.commit()
            raise


def run_event_cluster_from_settings(
    *,
    settings: Settings,
    run_id: int,
    ai_client: Any | None = None,
    force: bool = False,
    now: datetime | None = None,
    reference_time: datetime | None = None,
) -> EventClusterResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    client = ai_client or StageCAggregationClient.from_settings(settings)
    return run_event_cluster_job(
        session_factory=create_session_factory(engine),
        ai_client=client,
        run_id=run_id,
        force=force,
        now=now,
        reference_time=reference_time,
        input_min_score=settings.ai_stage_c_input_min_score,
    )


def _load_cluster_items(
    session: Session,
    *,
    run_id: int,
    input_min_score: int,
) -> _StageCInputSelection:
    """Build Stage C's auditable, deterministic input set from Stage-B items."""

    stage = session.scalar(
        select(IntelRunStage).where(
            IntelRunStage.run_id == int(run_id),
            IntelRunStage.stage_name == "analyze",
        )
    )
    if stage is None:
        raise ValueError("Stage C requires the Stage B analysis stage")
    if str(stage.status) in {"pending", "running", "retry_waiting"}:
        raise ValueError("Stage C requires the Stage B analysis stage to finish before aggregation")
    if str(stage.status) not in {"succeeded", "failed", "blocked"}:
        raise ValueError("Stage C requires a terminal Stage B analysis stage")

    item_tasks = list(
        session.scalars(
            select(IntelRunStageTask)
            .where(
                IntelRunStageTask.stage_id == stage.id,
                IntelRunStageTask.subject_type == "item",
            )
            .order_by(IntelRunStageTask.id.asc())
        ).all()
    )
    active_statuses = {"pending", "running", "retry_waiting"}
    if any(str(task.status) in active_statuses for task in item_tasks):
        raise ValueError("Stage C requires Stage B item tasks to finish before aggregation")

    status_counts: dict[str, int] = {}
    succeeded_item_ids: list[int] = []
    succeeded_tasks_by_item: dict[int, IntelRunStageTask] = {}
    for task in item_tasks:
        status = str(task.status)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "succeeded":
            continue
        item_id = task.item_id
        if item_id is None and str(task.subject_id).isdigit():
            item_id = int(task.subject_id)
        if item_id is not None and int(item_id) > 0 and int(item_id) not in succeeded_item_ids:
            succeeded_item_ids.append(int(item_id))
            succeeded_tasks_by_item[int(item_id)] = task

    items_by_id: dict[int, IntelItem] = {}
    if succeeded_item_ids:
        stmt = (
            select(IntelItem)
            .join(IntelRunItem, IntelRunItem.item_id == IntelItem.id)
            .where(
                IntelRunItem.run_id == int(run_id),
                IntelItem.id.in_(succeeded_item_ids),
            )
            .options(
                joinedload(IntelItem.source),
                joinedload(IntelItem.ai_review),
            )
        )
        items_by_id = {
            int(item.id): item
            for item in session.scalars(stmt).unique().all()
        }

    excluded: dict[str, list[int]] = {
        "analysis_filtered": [],
        "below_min_score": [],
        "missing_item": [],
        "missing_review": [],
    }
    selected: list[IntelItem] = []
    for item_id in succeeded_item_ids:
        item = items_by_id.get(item_id)
        if item is None:
            excluded["missing_item"].append(item_id)
            continue
        review = item.ai_review
        if (
            item.status == "analysis_filtered"
            or _stage_b_task_is_structurally_filtered(succeeded_tasks_by_item[item_id])
            or _review_is_structurally_filtered(review)
        ):
            excluded["analysis_filtered"].append(item_id)
            continue
        if review is None or review.status != "success":
            excluded["missing_review"].append(item_id)
            continue
        if int(review.selection_score or 0) < input_min_score:
            excluded["below_min_score"].append(item_id)
            continue
        selected.append(item)

    audit = {
        "policy_version": STAGE_C_INPUT_POLICY_VERSION,
        "min_score": input_min_score,
        "stage_b_task_statuses": status_counts,
        "stage_b_succeeded": len(succeeded_item_ids),
        "selected_count": len(selected),
        "selected_item_ids": [int(item.id) for item in selected],
        "excluded_item_ids": excluded,
        "excluded_counts": {reason: len(item_ids) for reason, item_ids in excluded.items()},
    }
    return _StageCInputSelection(items=selected, audit=audit)


def _stage_b_task_is_structurally_filtered(task: IntelRunStageTask) -> bool:
    result = task.result
    if not isinstance(result, Mapping):
        return False
    if bool(result.get("filtered")):
        return True
    return bool(result.get("analysis_filtered_reason"))


def _review_is_structurally_filtered(review: Any) -> bool:
    # Structural filtering is recorded on the Stage-B task result.  The B1
    # review projection no longer has a free-form reason field.
    del review
    return False


def _load_published_daily_history(
    session: Session,
    *,
    run: IntelRun,
    days: int,
) -> list[dict[str, Any]]:
    if days <= 0 or run.edition_id is None or not run.edition_date:
        return []
    current_edition = date.fromisoformat(run.edition_date)
    earliest = current_edition - timedelta(days=days)
    rows = session.execute(
        select(DailyEditionReportEntry, DailyEdition)
        .join(DailyEdition, DailyEdition.id == DailyEditionReportEntry.edition_id)
        .where(
            DailyEdition.edition_date >= earliest,
            DailyEdition.edition_date < current_edition,
            DailyEdition.published_at.is_not(None),
        )
        .order_by(DailyEdition.edition_date.desc(), DailyEditionReportEntry.display_order.asc())
    ).all()
    return [
        {
            "event_key": entry.event_key,
            "edition_date": edition.edition_date.isoformat(),
            "title": entry.original_title or entry.title,
            "summary_cn": entry.summary,
            "url": canonical_event_url(entry.url),
            "topic": entry.topic,
            "content_class": entry.content_class,
            "source_ids": entry.source_ids,
            "keywords": entry.keywords,
            "entities": entry.entities,
            "metadata": entry.metadata_dict,
        }
        for entry, edition in rows
    ]


def _item_candidate(item: IntelItem) -> dict[str, Any]:
    review = item.ai_review
    source = item.source
    topics = list(review.topics) if review is not None else []
    if review is not None and review.topic and review.topic not in topics:
        topics.insert(0, review.topic)
    return {
        "id": int(item.id),
        "source_id": item.source_id,
        "source_group": source.source_group if source else None,
        "source_role": source.source_role if source else None,
        "source_subtype": source.source_subtype if source else None,
        "primary_eligible": bool(source.primary_eligible) if source else False,
        "content_class": (review.content_class if review else None) or item.content_class,
        "canonical_url": canonical_event_url(item.canonical_url),
        "external_id": _normalize_external_id(item.external_id),
        "title": item.title,
        "summary_cn": (review.summary_cn if review else None) or item.summary,
        "topic": (review.topic if review else None) or (topics[0] if topics else None),
        "topics": _clean_strings(topics),
        "keywords": list(review.keywords) if review is not None else [],
        "entities": list(review.entities) if review is not None else [],
        "selection_score": _number(review.selection_score if review else item.selection_score),
        "published_at": _iso_datetime(item.published_at or item.discovered_at or item.captured_at),
        "captured_at": _iso_datetime(item.captured_at),
        "identity_keys": exact_identity_keys(item),
    }


def _persist_ai_cluster(
    session: Session,
    *,
    run_id: int,
    current: datetime,
    cluster: StageCStoryCluster,
    candidates: Mapping[int, Mapping[str, Any]],
    request_metadata: Mapping[str, Any],
):
    repo = IntelRepository(session)
    members = [candidates[member.item_id] for member in cluster.members]
    primary = candidates[cluster.primary_item_id]
    times = [_as_utc(row.get("published_at") or row.get("captured_at")) for row in members]
    times = [value for value in times if value is not None]
    event_key = cluster.prior_event_key or canonical_event_key(primary)
    resolution_raw = {
        "schema_version": STAGE_C_SCHEMA_VERSION,
        "cluster": cluster.model_dump(mode="json"),
        "request_metadata": dict(request_metadata),
    }
    event = repo.upsert_event(
        run_id=run_id,
        event_key=event_key,
        canonical_url=primary.get("canonical_url"),
        external_id=primary.get("external_id"),
        normalized_title=normalize_event_title(cluster.title_zh),
        title=cluster.title_zh,
        summary_cn=cluster.summary_zh,
        topic=primary.get("topic") or "technology_insight",
        topics=_clean_strings(topic for row in members for topic in [row.get("topic"), *row.get("topics", [])]),
        keywords=_unique_strings(keyword for row in members for keyword in row.get("keywords", [])),
        entities=_unique_json_objects(entity for row in members for entity in row.get("entities", [])),
        content_class=primary.get("content_class"),
        source_group=primary.get("source_group"),
        source_ids=_unique_strings(row.get("source_id") for row in members),
        source_groups=_unique_strings(row.get("source_group") for row in members),
        identity_keys=_unique_strings(key for row in members for key in row.get("identity_keys", ())),
        display_score=max((_number(row.get("selection_score")) for row in members), default=0.0),
        novelty_status=cluster.novelty_status,
        state="candidate",
        resolution_method="ai_story_aggregation",
        resolution_confidence=100,
        resolution_raw=resolution_raw,
        primary_item_id=cluster.primary_item_id,
        first_seen_at=min(times) if times else current,
        last_seen_at=max(times) if times else current,
    )
    event.primary_item_id = cluster.primary_item_id
    for member in cluster.members:
        row = candidates[member.item_id]
        repo.upsert_event_item(
            int(event.id),
            member.item_id,
            source_id=str(row.get("source_id") or ""),
            source_group=_text(row.get("source_group")),
            identity_key=next(iter(row.get("identity_keys", ())), None),
            match_type=member.relation,
            match_confidence=100,
            is_primary=member.relation == "primary",
            lineage={
                "run_id": run_id,
                "relation": member.relation,
                "source_id": row.get("source_id"),
                "source_group": row.get("source_group"),
                "canonical_url": row.get("canonical_url"),
                "external_id": row.get("external_id"),
                "title": row.get("title"),
            },
        )
    session.flush()
    return event


def _clear_build_events(session: Session, *, run_id: int) -> None:
    """Replace the draft's prior Stage-C projection only after AI validation."""

    events = session.scalars(
        select(IntelEvent).where(IntelEvent.build_id == int(run_id))
    ).all()
    for event in events:
        session.delete(event)
    session.flush()


def _cluster_input_fingerprint(
    candidates: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
) -> str:
    payload = {"candidates": list(candidates), "history": list(history)}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stage_c_config_fingerprint(*, model_name: str, input_min_score: int) -> str:
    payload = {
        "prompt_version": STAGE_C_PROMPT_VERSION,
        "model": model_name,
        "input_policy_version": STAGE_C_INPUT_POLICY_VERSION,
        "input_min_score": input_min_score,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_id_list(value: Any) -> list[int]:
    values = value if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)) else ()
    result: list[int] = []
    for item in values:
        try:
            event_id = int(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if event_id > 0 and event_id not in result:
            result.append(event_id)
    return result


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip() if value is not None else ""
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
    return _unique_strings(values)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="python"))
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


def _bounded_score(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


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


__all__ = [
    "EventClusterResult",
    "canonical_event_key",
    "canonical_event_url",
    "exact_identity_keys",
    "normalize_event_title",
    "run_event_cluster_from_settings",
    "run_event_cluster_job",
]
