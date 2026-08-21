"""Private build summary data used while publishing an edition.

The pipeline keeps durable detail in run items and generic stage tasks.
This module derives the compact funnel/status projection needed by a daily
manifest without relying on aggregate counter columns on :class:`IntelRun`.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.storage.models import (
    IntelRun,
    IntelRunItem,
    IntelRunStage,
    IntelRunStageTask,
)


CANONICAL_STAGE_NAMES = ("fetch", "screen", "analyze", "cluster", "stage_d", "export")
TASK_STATUS_NAMES = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "retry_waiting",
    "blocked",
    "skipped",
    "cancelled",
)
TIME_EXCLUSION_REASONS = ("too_old", "future_timestamp", "missing_published_at")


def build_daily_build_summary(
    session: Session,
    *,
    run: IntelRun,
    export_status: str | None = None,
    export_error: str | None = None,
) -> dict[str, Any]:
    """Return a safe, build-scoped manifest summary for one Stage-D result.

    The result intentionally contains counts and structured error summaries
    only.  It never includes collector payloads, AI raw responses or stage
    task result blobs.
    """

    run_id = int(run.id)
    stages = list(
        session.scalars(
            select(IntelRunStage).where(IntelRunStage.run_id == run_id)
        ).all()
    )
    stages_by_name = {stage.stage_name: stage for stage in stages}
    tasks_by_stage = _tasks_by_stage(session, stages)
    run_item_statuses = _run_item_statuses(session, run_id)
    frozen = int(
        session.execute(
            select(func.count(IntelRunItem.id)).where(
                IntelRunItem.run_id == run_id,
            )
        ).scalar_one()
    )

    stages_payload: dict[str, dict[str, Any]] = {}
    failure_reasons: list[dict[str, str]] = []
    for stage_name in CANONICAL_STAGE_NAMES:
        stage = stages_by_name.get(stage_name)
        stage_tasks = tasks_by_stage.get(stage.id, []) if stage is not None else []
        task_counts = _task_counts(stage_tasks)
        status = stage.status if stage is not None else "missing"
        error = _error_payload(stage, stage_tasks)
        if stage_name == "export" and export_status:
            status = export_status
            task_counts = _terminal_export_task_counts(task_counts, export_status)
            if export_error:
                error = {"category": "upstream", "code": "partial_upstream", "message": export_error}
        details = _stage_details(stage_name, stage)
        stages_payload[stage_name] = {
            "status": status,
            "started_at": _iso(stage.started_at) if stage is not None else None,
            "finished_at": _iso(stage.finished_at) if stage is not None else None,
            "reference_time": _iso(stage.reference_time) if stage is not None else None,
            "task_counts": task_counts,
            "error": error,
            "details": details,
        }
        if status in {"failed", "blocked", "partial"} or error is not None:
            _append_failure(failure_reasons, stage=stage_name, status=status, error=error)

    screen_tasks = tasks_by_stage.get(stages_by_name["screen"].id, []) if "screen" in stages_by_name else []
    analyze_tasks = tasks_by_stage.get(stages_by_name["analyze"].id, []) if "analyze" in stages_by_name else []
    cluster_tasks = tasks_by_stage.get(stages_by_name["cluster"].id, []) if "cluster" in stages_by_name else []
    stage_d_tasks = tasks_by_stage.get(stages_by_name["stage_d"].id, []) if "stage_d" in stages_by_name else []

    time_excluded_by_reason = _time_exclusion_counts(screen_tasks)
    time_excluded = sum(time_excluded_by_reason.values())
    screen_task_count = len(screen_tasks)
    if not time_excluded and stages_by_name.get("screen") is not None:
        metadata_counts = stages_by_name["screen"].metadata_dict.get("freshness_counts")
        if isinstance(metadata_counts, dict):
            time_excluded_by_reason = {
                reason: _as_nonnegative_int(metadata_counts.get(reason))
                for reason in TIME_EXCLUSION_REASONS
            }
            time_excluded = sum(time_excluded_by_reason.values())

    stage_a_pass, stage_a_rejected = _screen_decision_counts(screen_tasks)
    stage_a_excluded = run_item_statuses.get("screened_out", 0) or stage_a_rejected
    stage_a_failed = max(
        run_item_statuses.get("screen_failed", 0),
        _failed_task_count(screen_tasks),
    )
    stage_b_successful, stage_b_structurally_filtered = _analysis_counts(analyze_tasks)
    stage_b_analyzed = stage_b_successful + stage_b_structurally_filtered
    if not stage_b_analyzed:
        stage_b_analyzed = run_item_statuses.get("candidate", 0)
    if not stage_b_structurally_filtered:
        stage_b_structurally_filtered = run_item_statuses.get("analysis_filtered", 0)
    stage_c_input = _stage_c_input_counts(cluster_tasks)
    stage_b_failed = max(
        run_item_statuses.get("analysis_failed", 0),
        _failed_task_count(analyze_tasks),
    )

    stage_d_total, stage_d_selected = _stage_d_counts(stage_d_tasks)
    stage_d_metadata = stages_by_name.get("stage_d").metadata_dict if stages_by_name.get("stage_d") else {}
    stage_d_candidate_count = max(
        stage_d_total,
        _as_nonnegative_int(stage_d_metadata.get("candidate_count")),
    )
    fetch_metadata = stages_by_name.get("fetch").metadata_dict if stages_by_name.get("fetch") else {}
    fetched = _as_nonnegative_int(fetch_metadata.get("fetched")) or frozen
    inserted = _as_nonnegative_int(fetch_metadata.get("inserted")) or frozen
    within_72h = screen_task_count - sum(
        1 for task in screen_tasks if task.status == "skipped"
    )
    scope_items = frozen
    if not screen_task_count:
        within_72h = max(0, scope_items - time_excluded)

    funnel = {
        "fetched": fetched,
        "inserted": inserted,
        "frozen": frozen,
        "within_72h": max(0, within_72h),
        "time_excluded": time_excluded,
        "time_excluded_by_reason": time_excluded_by_reason,
        "stage_a_pass": stage_a_pass,
        "stage_a_excluded": stage_a_excluded,
        "stage_a_failed": stage_a_failed,
        "stage_b_analyzed": stage_b_analyzed,
        "stage_b_structurally_filtered": stage_b_structurally_filtered,
        "stage_b_failed": stage_b_failed,
        "stage_c_input_selected": stage_c_input["selected"],
        "stage_c_input_below_min_score": stage_c_input["below_min_score"],
        "stage_c_input_structurally_filtered": stage_c_input["analysis_filtered"],
        "stage_c_input_invalid_contract": stage_c_input["invalid_contract"],
        "stage_c_events": _cluster_event_count(cluster_tasks),
        "stage_d_total": stage_d_total,
        "stage_d_candidate_count": stage_d_candidate_count,
        "stage_d_selected": stage_d_selected,
    }
    funnel["full_rebuild_items"] = frozen
    return {
        "funnel": funnel,
        "stages": stages_payload,
        "failure_reasons": failure_reasons,
    }


def _tasks_by_stage(
    session: Session,
    stages: list[IntelRunStage],
) -> dict[int, list[IntelRunStageTask]]:
    if not stages:
        return {}
    stage_ids = [stage.id for stage in stages]
    result: dict[int, list[IntelRunStageTask]] = {stage_id: [] for stage_id in stage_ids}
    for task in session.scalars(
        select(IntelRunStageTask)
        .where(IntelRunStageTask.stage_id.in_(stage_ids))
        .order_by(IntelRunStageTask.id.asc())
    ).all():
        result.setdefault(task.stage_id, []).append(task)
    return result


def _run_item_statuses(session: Session, run_id: int) -> dict[str, int]:
    return {
        str(status): int(count)
        for status, count in session.execute(
            select(IntelRunItem.status, func.count(IntelRunItem.id))
            .where(IntelRunItem.run_id == run_id)
            .group_by(IntelRunItem.status)
        ).all()
        if status
    }


def _task_counts(tasks: list[IntelRunStageTask]) -> dict[str, int]:
    counts = Counter(str(task.status) for task in tasks)
    return {status: int(counts.get(status, 0)) for status in TASK_STATUS_NAMES}


def _terminal_export_task_counts(counts: dict[str, int], export_status: str) -> dict[str, int]:
    """Project the export lease into the state written immediately after I/O."""

    result = dict(counts)
    running = result.get("running", 0)
    if not running:
        return result
    result["running"] = 0
    if export_status == "succeeded":
        result["succeeded"] = result.get("succeeded", 0) + running
    elif export_status == "partial":
        result["retry_waiting"] = result.get("retry_waiting", 0) + running
    return result


def _stage_details(
    stage_name: str,
    stage: IntelRunStage | None,
) -> dict[str, Any]:
    if stage is None:
        return {}
    metadata = stage.metadata_dict
    if stage_name == "fetch":
        return {
            key: _as_nonnegative_int(metadata.get(key))
            for key in ("fetched", "inserted", "failed")
            if metadata.get(key) is not None
        }
    if stage_name == "screen":
        details: dict[str, Any] = {}
        window_hours = metadata.get("freshness_window_hours", metadata.get("window_hours"))
        if window_hours is not None:
            details["window_hours"] = _as_nonnegative_int(window_hours)
        for key in ("cutoff_at", "reference_time", "freshness_policy"):
            if metadata.get(key) is not None:
                details[key] = metadata[key]
        freshness_counts = metadata.get("freshness_counts")
        if isinstance(freshness_counts, dict):
            details["freshness_counts"] = {
                str(key): _as_nonnegative_int(value)
                for key, value in freshness_counts.items()
            }
        return details
    if stage_name == "stage_d":
        details: dict[str, Any] = {}
        for key in (
            "stage_d_version",
            "profile_version",
            "prompt_version",
            "schema_version",
            "candidate_count",
            "selected_count",
            "unselected_count",
            "max_selected",
            "model",
            "provider_attempts",
        ):
            value = metadata.get(key)
            if value is not None:
                details[key] = value
        return details
    if stage_name in {"cluster", "export"}:
        # These stages expose their public state through the edition manifest.
        return {}
    return {}


def _error_payload(
    stage: IntelRunStage | None,
    tasks: list[IntelRunStageTask],
) -> dict[str, str] | None:
    for row in [stage, *tasks]:
        if row is None:
            continue
        category = getattr(row, "error_category", None)
        code = getattr(row, "error_code", None)
        message = getattr(row, "error_message", None)
        if category or code or message:
            return {
                "category": str(category or "unknown"),
                "code": str(code or "unknown"),
                "message": _truncate(message),
            }
    if stage is not None and stage.stage_name == "fetch":
        failed = _as_nonnegative_int(stage.metadata_dict.get("failed"))
        if failed:
            return {
                "category": "fetch",
                "code": "source_failures",
                "message": f"{failed} source failures",
            }
    return None


def _append_failure(
    target: list[dict[str, str]],
    *,
    stage: str,
    status: str,
    error: dict[str, str] | None,
) -> None:
    value = {"stage": stage, "status": status}
    if error is not None:
        value.update(error)
    if value not in target:
        target.append(value)


def _time_exclusion_counts(tasks: list[IntelRunStageTask]) -> dict[str, int]:
    counts = {reason: 0 for reason in TIME_EXCLUSION_REASONS}
    for task in tasks:
        if task.status != "skipped":
            continue
        reason = _normalise_time_reason(_result_mapping(task).get("reason"))
        if reason:
            counts[reason] += 1
    return counts


def _screen_decision_counts(tasks: list[IntelRunStageTask]) -> tuple[int, int]:
    passed = 0
    rejected = 0
    for task in tasks:
        if task.status != "succeeded":
            continue
        decision = str(_result_mapping(task).get("decision") or "").casefold()
        if decision in {"pass", "uncertain"}:
            passed += 1
        elif decision in {"reject", "excluded"}:
            rejected += 1
    return passed, rejected


def _analysis_counts(tasks: list[IntelRunStageTask]) -> tuple[int, int]:
    analyzed = 0
    filtered = 0
    for task in tasks:
        if task.subject_type != "item":
            continue
        if task.status != "succeeded":
            continue
        result = _result_mapping(task)
        if bool(result.get("filtered")) or bool(result.get("analysis_filtered_reason")):
            filtered += 1
        else:
            analyzed += 1
    return analyzed, filtered


def _stage_c_input_counts(tasks: list[IntelRunStageTask]) -> dict[str, int]:
    """Read the Stage-C input audit persisted alongside its aggregation task."""

    empty = {
        "selected": 0,
        "below_min_score": 0,
        "analysis_filtered": 0,
        "invalid_contract": 0,
    }
    for task in tasks:
        if task.subject_type != "run" or task.status != "succeeded":
            continue
        audit = _result_mapping(task).get("input_audit")
        if not isinstance(audit, dict):
            continue
        excluded_counts = audit.get("excluded_counts")
        if not isinstance(excluded_counts, dict):
            excluded_counts = {}
        return {
            "selected": _as_nonnegative_int(audit.get("selected_count")),
            "below_min_score": _as_nonnegative_int(excluded_counts.get("below_min_score")),
            "analysis_filtered": _as_nonnegative_int(excluded_counts.get("analysis_filtered")),
            "invalid_contract": sum(
                _as_nonnegative_int(excluded_counts.get(key))
                for key in ("missing_item", "missing_review")
            ),
        }
    return empty


def _failed_task_count(tasks: list[IntelRunStageTask]) -> int:
    return sum(task.status in {"failed", "retry_waiting", "blocked"} for task in tasks)


def _cluster_event_count(tasks: list[IntelRunStageTask]) -> int:
    for task in tasks:
        if task.subject_type != "run":
            continue
        result = _result_mapping(task)
        values = result.get("current_event_ids") or result.get("event_ids") or []
        if isinstance(values, (list, tuple, set)):
            return len({int(value) for value in values if _is_int_like(value)})
    return 0


def _stage_d_counts(tasks: list[IntelRunStageTask]) -> tuple[int, int]:
    for task in tasks:
        if task.subject_type != "run" or task.status != "succeeded":
            continue
        result = _result_mapping(task)
        candidates = result.get("candidate_event_ids")
        selected = result.get("selected")
        total_count = len(candidates) if isinstance(candidates, list) else 0
        selected_count = len(selected) if isinstance(selected, list) else 0
        return total_count, selected_count
    return 0, 0


def _result_mapping(task: IntelRunStageTask) -> dict[str, Any]:
    result = task.result
    return dict(result) if isinstance(result, dict) else {}


def _normalise_time_reason(value: object) -> str | None:
    reason = str(value or "").casefold()
    if "future" in reason:
        return "future_timestamp"
    if "missing" in reason and "publish" in reason:
        return "missing_published_at"
    if "old" in reason or "expired" in reason or "window" in reason:
        return "too_old"
    return None


def _as_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _is_int_like(value: object) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _truncate(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
