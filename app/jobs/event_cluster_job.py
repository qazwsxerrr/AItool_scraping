"""Stage C: bounded AI aggregation of successful Stage-B projections."""

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
    StageCAggregationProviderError,
    StageCStoryCluster,
    strict_parse_stage_c_aggregation,
)
from app.config.limits import (
    DEFAULT_AI_STAGE_C_INPUT_MIN_SCORE,
    STAGE_C_AGGREGATION_MODE,
    STAGE_C_BATCH_INPUT_BYTE_LIMIT,
    STAGE_C_BATCH_ITEM_LIMIT,
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
STAGE_C_CANDIDATE_CONTRACT_VERSION = "stage_c_candidate_events_v1"
_TRACKING_QUERY_KEYS = {"ref", "source", "src", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid"}
_STAGE_C_PRIMARY_POLICY_VERSION = "source_then_b1_priority_v1"


class StageCDownstreamBusyError(RuntimeError):
    """A live Stage-D/export worker prevents safe Stage-C replacement."""


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
    candidate_event_ids: list[int] = field(default_factory=list)
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
    batch_item_limit: int = STAGE_C_BATCH_ITEM_LIMIT,
    batch_input_byte_limit: int = STAGE_C_BATCH_INPUT_BYTE_LIMIT,
) -> EventClusterResult:
    """Aggregate the current Stage-B item set through bounded AI requests."""

    result = EventClusterResult(run_id=int(run_id))
    owner = "stage-c-ai-aggregation"
    raw_response: Any | None = None
    request_metadata: Mapping[str, Any] | None = None
    min_score = _bounded_score(input_min_score, DEFAULT_AI_STAGE_C_INPUT_MIN_SCORE)
    resolved_batch_item_limit = _positive_int(batch_item_limit, STAGE_C_BATCH_ITEM_LIMIT)
    resolved_batch_input_byte_limit = _positive_int(
        batch_input_byte_limit,
        STAGE_C_BATCH_INPUT_BYTE_LIMIT,
    )
    model_name = str(getattr(ai_client, "model", None) or "custom")
    config_fingerprint = _stage_c_config_fingerprint(
        model_name=model_name,
        input_min_score=min_score,
        batch_item_limit=resolved_batch_item_limit,
        batch_input_byte_limit=resolved_batch_input_byte_limit,
    )

    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None or run.edition_id is None:
            raise ValueError("Stage C requires the current daily edition build")
        _assert_downstream_idle(repo, int(run_id))
        frozen_reference = _as_utc(reference_time) or _as_utc(run.reference_time) or _as_utc(now)
        frozen_reference = frozen_reference or datetime.now(timezone.utc)
        stage = repo.ensure_stage(
            int(run_id),
            "cluster",
            config_fingerprint=config_fingerprint,
            reference_time=frozen_reference,
            metadata={
                "aggregation_mode": STAGE_C_AGGREGATION_MODE,
                "history_mode": "prior_published_daily_reports",
                "daily_history_days": DAILY_HISTORY_DAYS,
                "prompt_version": STAGE_C_PROMPT_VERSION,
                "candidate_contract_version": STAGE_C_CANDIDATE_CONTRACT_VERSION,
                "input_policy_version": STAGE_C_INPUT_POLICY_VERSION,
                "input_min_score": min_score,
                "batch_item_limit": resolved_batch_item_limit,
                "batch_input_byte_limit": resolved_batch_input_byte_limit,
                "primary_policy_version": _STAGE_C_PRIMARY_POLICY_VERSION,
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
                result.candidate_event_ids = _event_id_list(stored.get("candidate_event_ids"))
                result.input_audit = _mapping(stored.get("input_audit"))
                result.processed = 0
                return result
            raise RuntimeError("Stage C task is already running")
        task = claimed
        try:
            repo.invalidate_downstream_stages(
                int(run_id),
                stage_names=("stage_d", "export"),
                upstream_stage="cluster",
            )
        except RuntimeError as exc:
            session.rollback()
            if str(exc).startswith("downstream_stage_busy:"):
                raise StageCDownstreamBusyError(str(exc)) from exc
            raise
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
                        "candidate_event_ids": [],
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
            call = _aggregate_stage_c_batches(
                ai_client=ai_client,
                candidates=candidates,
                recent_history=history,
                edition={
                    "edition_date": run.edition_date,
                    "reference_time": current.isoformat(),
                },
                batch_item_limit=resolved_batch_item_limit,
                batch_input_byte_limit=resolved_batch_input_byte_limit,
            )
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
                primary_item_id = _select_primary_item_id(cluster.item_ids, candidates=by_id)
                event_key = cluster.prior_event_key or canonical_event_key(by_id[primary_item_id])
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
                result.merged += max(0, len(cluster.item_ids) - 1)
                if cluster.novelty_status == "new":
                    result.events += 1
                    result.event_ids.append(int(event.id))
                    result.candidate_event_ids.append(int(event.id))
                elif cluster.novelty_status == "updated":
                    result.updated += 1
                    result.candidate_event_ids.append(int(event.id))
                else:
                    result.repeats += 1

            repo.complete_stage_task(
                task,
                owner=owner,
                result={
                    "schema_version": STAGE_C_SCHEMA_VERSION,
                    "event_ids": result.event_ids,
                    "current_event_ids": result.current_event_ids,
                    "candidate_event_ids": result.candidate_event_ids,
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
                        retryable=(
                            bool(getattr(exc, "retryable", False))
                            or isinstance(exc, StageCDownstreamBusyError)
                            or str(exc).startswith("downstream_stage_busy:")
                        ),
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
        if int(review.b1_priority or 0) < input_min_score:
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
        "b1_priority": _number(review.b1_priority if review else item.b1_priority),
        "published_at": _iso_datetime(item.published_at or item.discovered_at or item.captured_at),
        "captured_at": _iso_datetime(item.captured_at),
        "identity_keys": exact_identity_keys(item),
    }


def _aggregate_stage_c_batches(
    *,
    ai_client: Any,
    candidates: Sequence[Mapping[str, Any]],
    recent_history: Sequence[Mapping[str, Any]],
    edition: Mapping[str, Any],
    batch_item_limit: int,
    batch_input_byte_limit: int,
) -> StageCAggregationCallResult:
    """Run independently valid bounded calls, then validate their exact union."""

    # History is repeated in every request. Reserve its conservative serialized
    # footprint before packing current candidates, rather than letting a large
    # three-day history silently defeat the request budget.
    current_item_byte_budget = max(1, batch_input_byte_limit - _serialized_size(recent_history))
    batches = _partition_stage_c_candidates(
        candidates,
        item_limit=batch_item_limit,
        input_byte_limit=current_item_byte_budget,
    )
    batch_records: list[dict[str, Any]] = []
    raw_batches: list[dict[str, Any]] = []
    clusters: list[StageCStoryCluster] = []
    expected_history_keys = [str(row["event_key"]) for row in recent_history]

    for batch_index, batch in enumerate(batches, start=1):
        item_ids = [int(row["id"]) for row in batch]
        try:
            call = ai_client.aggregate(
                batch,
                recent_history=recent_history,
                edition=edition,
            )
            if not isinstance(call, StageCAggregationCallResult):
                raise TypeError("Stage C aggregate() must return StageCAggregationCallResult")
            parsed = strict_parse_stage_c_aggregation(
                call.parsed.model_dump(mode="json"),
                item_ids=item_ids,
                prior_event_keys=expected_history_keys,
            )
        except Exception as exc:
            failed_metadata = _stage_c_batch_request_metadata(
                batch_records,
                candidate_count=len(candidates),
                history_count=len(recent_history),
                batch_item_limit=batch_item_limit,
                batch_input_byte_limit=batch_input_byte_limit,
                history_link_merges=(),
                failed_batch_index=batch_index,
            )
            failed_raw_response = getattr(exc, "raw_response", None)
            raise StageCAggregationProviderError(
                str(exc),
                status_code=getattr(exc, "status_code", None),
                error_code=getattr(exc, "error_code", None) or "batch_aggregation_failed",
                raw_response={
                    "batch_responses": raw_batches,
                    "failed_batch": {
                        "batch_index": batch_index,
                        "item_ids": item_ids,
                        "provider_response": failed_raw_response,
                    },
                },
                request_metadata=failed_metadata,
                cause=exc,
            ) from exc

        request_metadata = dict(call.request_metadata)
        batch_records.append(
            {
                "batch_index": batch_index,
                "item_ids": item_ids,
                "item_count": len(item_ids),
                "request_metadata": request_metadata,
            }
        )
        raw_batches.append(
            {
                "batch_index": batch_index,
                "item_ids": item_ids,
                "provider_response": call.raw_response,
            }
        )
        clusters.extend(parsed.clusters)

    candidates_by_id = {int(row["id"]): row for row in candidates}
    merged_clusters, history_link_merges = _merge_history_linked_clusters(
        clusters,
        candidates=candidates_by_id,
    )
    combined = strict_parse_stage_c_aggregation(
        {
            "schema_version": STAGE_C_SCHEMA_VERSION,
            "clusters": [cluster.model_dump(mode="json") for cluster in merged_clusters],
        },
        item_ids=[int(row["id"]) for row in candidates],
        prior_event_keys=expected_history_keys,
    )
    aggregate_metadata = _stage_c_batch_request_metadata(
        batch_records,
        candidate_count=len(candidates),
        history_count=len(recent_history),
        batch_item_limit=batch_item_limit,
        batch_input_byte_limit=batch_input_byte_limit,
        history_link_merges=history_link_merges,
    )
    return StageCAggregationCallResult(
        parsed=combined,
        raw_response={"batch_responses": raw_batches},
        request_metadata=aggregate_metadata,
    )


def _partition_stage_c_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    item_limit: int,
    input_byte_limit: int,
) -> list[list[Mapping[str, Any]]]:
    """Co-locate likely related records without deciding their final grouping."""

    ordered = sorted(candidates, key=_stage_c_batch_key)
    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    current_bytes = 0
    for candidate in ordered:
        candidate_bytes = _serialized_size(candidate)
        would_exceed_item_limit = len(current) >= item_limit
        would_exceed_byte_limit = bool(current) and current_bytes + candidate_bytes > input_byte_limit
        if current and (would_exceed_item_limit or would_exceed_byte_limit):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(candidate)
        current_bytes += candidate_bytes
    if current:
        batches.append(current)
    return batches


def _stage_c_batch_key(candidate: Mapping[str, Any]) -> tuple[str, str, int]:
    typed_entities: list[tuple[int, str]] = []
    entity_rank = {
        "model": 0,
        "product": 1,
        "project": 2,
        "organization": 3,
        "company": 4,
    }
    entities = candidate.get("entities")
    if isinstance(entities, Iterable) and not isinstance(entities, (str, bytes, Mapping)):
        for entity in entities:
            if not isinstance(entity, Mapping):
                continue
            name = normalize_event_title(entity.get("name"))
            if not name:
                continue
            entity_type = str(entity.get("type") or "").strip().casefold()
            typed_entities.append((entity_rank.get(entity_type, 9), name))
    if typed_entities:
        rank, name = min(typed_entities)
        anchor = f"entity:{rank}:{name}"
    else:
        keyword_values = candidate.get("keywords")
        keywords = (
            [normalize_event_title(value) for value in keyword_values]
            if isinstance(keyword_values, Iterable) and not isinstance(keyword_values, (str, bytes, Mapping))
            else []
        )
        keywords = [value for value in keywords if value]
        if keywords:
            anchor = f"keyword:{min(keywords, key=lambda value: (-len(value), value))}"
        else:
            anchor = f"topic:{normalize_event_title(candidate.get('topic'))}"
    title = normalize_event_title(candidate.get("title"))
    return anchor, title, int(candidate["id"])


def _merge_history_linked_clusters(
    clusters: Sequence[StageCStoryCluster],
    *,
    candidates: Mapping[int, Mapping[str, Any]],
) -> tuple[list[StageCStoryCluster], list[dict[str, Any]]]:
    """Merge only clusters whose model output names the same published event."""

    by_history_key: dict[str, list[StageCStoryCluster]] = {}
    for cluster in clusters:
        if cluster.prior_event_key:
            by_history_key.setdefault(cluster.prior_event_key, []).append(cluster)

    merged: list[StageCStoryCluster] = []
    audit: list[dict[str, Any]] = []
    processed_history_keys: set[str] = set()
    for cluster in clusters:
        history_key = cluster.prior_event_key
        if not history_key:
            merged.append(cluster)
            continue
        if history_key in processed_history_keys:
            continue
        processed_history_keys.add(history_key)
        linked = by_history_key[history_key]
        if len(linked) == 1:
            merged.append(cluster)
            continue
        item_ids = [item_id for current in linked for item_id in current.item_ids]
        primary_item_id = _select_primary_item_id(item_ids, candidates=candidates)
        representative = next(current for current in linked if primary_item_id in current.item_ids)
        novelty_status = "updated" if any(current.novelty_status == "updated" for current in linked) else "repeat"
        merged.append(
            StageCStoryCluster(
                title_zh=representative.title_zh,
                summary_zh=representative.summary_zh,
                item_ids=item_ids,
                novelty_status=novelty_status,
                prior_event_key=history_key,
            )
        )
        audit.append(
            {
                "prior_event_key": history_key,
                "source_cluster_count": len(linked),
                "item_ids": item_ids,
                "novelty_status": novelty_status,
            }
        )
    return merged, audit


def _stage_c_batch_request_metadata(
    batch_records: Sequence[Mapping[str, Any]],
    *,
    candidate_count: int,
    history_count: int,
    batch_item_limit: int,
    batch_input_byte_limit: int,
    history_link_merges: Sequence[Mapping[str, Any]],
    failed_batch_index: int | None = None,
) -> dict[str, Any]:
    normalized_records = [dict(row) for row in batch_records]
    request_bytes = sum(
        int(_number(_mapping(row.get("request_metadata")).get("request_bytes")))
        for row in normalized_records
    )
    digest_payload = [
        _mapping(row.get("request_metadata")).get("request_sha256")
        for row in normalized_records
    ]
    request_sha256 = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    result: dict[str, Any] = {
        "aggregation_mode": STAGE_C_AGGREGATION_MODE,
        "item_count": int(candidate_count),
        "history_count": int(history_count),
        "batch_count": len(normalized_records),
        "batch_item_limit": int(batch_item_limit),
        "batch_input_byte_limit": int(batch_input_byte_limit),
        "request_bytes": request_bytes,
        "request_sha256": request_sha256,
        "batches": normalized_records,
        "history_link_merges": [dict(row) for row in history_link_merges],
    }
    if failed_batch_index is not None:
        result["failed_batch_index"] = int(failed_batch_index)
    return result


def _select_primary_item_id(
    item_ids: Iterable[int],
    *,
    candidates: Mapping[int, Mapping[str, Any]],
) -> int:
    rows = [candidates[int(item_id)] for item_id in item_ids]
    if not rows:
        raise ValueError("Stage C cluster must contain at least one item_id")
    return int(min(rows, key=_primary_sort_key)["id"])


def _primary_sort_key(candidate: Mapping[str, Any]) -> tuple[int, int, float, float, int]:
    source_role = str(candidate.get("source_role") or "").strip().casefold()
    source_role_rank = {
        "official": 0,
        "first_party_official": 0,
        "publisher": 1,
        "news_media": 2,
        "analysis": 3,
        "code_hosting": 4,
        "community": 5,
        "social": 6,
    }.get(source_role, 7)
    published_at = _as_utc(candidate.get("published_at") or candidate.get("captured_at"))
    published_rank = -(published_at.timestamp() if published_at is not None else 0.0)
    return (
        0 if bool(candidate.get("primary_eligible")) else 1,
        source_role_rank,
        -_number(candidate.get("b1_priority")),
        published_rank,
        int(candidate["id"]),
    )


def _materialize_member_relations(
    item_ids: Iterable[int],
    *,
    primary_item_id: int,
    candidates: Mapping[int, Mapping[str, Any]],
) -> dict[int, str]:
    primary_keys = set(candidates[primary_item_id].get("identity_keys", ()))
    relations: dict[int, str] = {}
    for item_id in item_ids:
        item_id = int(item_id)
        if item_id == primary_item_id:
            relations[item_id] = "primary"
            continue
        item_keys = set(candidates[item_id].get("identity_keys", ()))
        relations[item_id] = "duplicate" if primary_keys and primary_keys & item_keys else "related"
    return relations


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


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
    primary_item_id = _select_primary_item_id(cluster.item_ids, candidates=candidates)
    member_relations = _materialize_member_relations(
        cluster.item_ids,
        primary_item_id=primary_item_id,
        candidates=candidates,
    )
    members = [candidates[item_id] for item_id in cluster.item_ids]
    primary = candidates[primary_item_id]
    times = [_as_utc(row.get("published_at") or row.get("captured_at")) for row in members]
    times = [value for value in times if value is not None]
    event_key = cluster.prior_event_key or canonical_event_key(primary)
    resolution_raw = {
        "schema_version": STAGE_C_SCHEMA_VERSION,
        "cluster": cluster.model_dump(mode="json"),
        "request_metadata": dict(request_metadata),
        "local_materialization": {
            "primary_item_id": primary_item_id,
            "member_relations": member_relations,
            "primary_policy_version": _STAGE_C_PRIMARY_POLICY_VERSION,
        },
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
        display_score=max((_number(row.get("b1_priority")) for row in members), default=0.0),
        novelty_status=cluster.novelty_status,
        state="candidate",
        resolution_method="ai_story_aggregation",
        resolution_confidence=100,
        resolution_raw=resolution_raw,
        primary_item_id=primary_item_id,
        first_seen_at=min(times) if times else current,
        last_seen_at=max(times) if times else current,
    )
    event.primary_item_id = primary_item_id
    for item_id, relation in member_relations.items():
        row = candidates[item_id]
        repo.upsert_event_item(
            int(event.id),
            item_id,
            source_id=str(row.get("source_id") or ""),
            source_group=_text(row.get("source_group")),
            identity_key=next(iter(row.get("identity_keys", ())), None),
            match_type=relation,
            match_confidence=100,
            is_primary=relation == "primary",
            lineage={
                "run_id": run_id,
                "relation": relation,
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


def _stage_c_config_fingerprint(
    *,
    model_name: str,
    input_min_score: int,
    batch_item_limit: int,
    batch_input_byte_limit: int,
) -> str:
    payload = {
        "prompt_version": STAGE_C_PROMPT_VERSION,
        "aggregation_mode": STAGE_C_AGGREGATION_MODE,
        "candidate_contract_version": STAGE_C_CANDIDATE_CONTRACT_VERSION,
        "primary_policy_version": _STAGE_C_PRIMARY_POLICY_VERSION,
        "model": model_name,
        "input_policy_version": STAGE_C_INPUT_POLICY_VERSION,
        "input_min_score": input_min_score,
        "batch_item_limit": batch_item_limit,
        "batch_input_byte_limit": batch_input_byte_limit,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _assert_downstream_idle(repo: IntelRepository, run_id: int) -> None:
    try:
        repo.assert_stages_idle(
            int(run_id),
            stage_names=("stage_d", "export"),
            upstream_stage="cluster",
        )
    except RuntimeError as exc:
        if str(exc).startswith("downstream_stage_busy:"):
            raise StageCDownstreamBusyError(str(exc)) from exc
        raise


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


def _positive_int(value: Any, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result > 0 else default


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
