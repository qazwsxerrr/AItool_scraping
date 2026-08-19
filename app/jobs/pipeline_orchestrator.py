"""Run-scoped orchestration for the resumable intelligence pipeline.

The individual jobs own their stage semantics.  This module only creates and
freezes a run scope, dispatches one named stage at a time, and exposes the
small control-plane operations used by the CLI (status, retry, resume and
adoption).  Keeping those decisions here prevents a retry command from
silently falling back to the legacy all-in-one AI job.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable

import httpx

from app.ai.skills.intel_triage import IntelTriageClient
from app.ai.skills.stage_d_editorial import StageDEditorialClient
from app.config.limits import (
    DEFAULT_AI_REVIEW_LIMIT,
    DEFAULT_DAILY_REPORT_LIMIT,
    DEFAULT_FETCH_LIMIT_PER_SOURCE,
)
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, load_source_registry
from app.domain.models import SourceSpec
from app.domain.recency import recent_window_scope
from app.jobs.ai_review_job import AIReviewResult, run_ai_review_from_settings
from app.jobs.event_cluster_job import EventClusterResult, run_event_cluster_from_settings
from app.jobs.export_job import IntelExportResult, run_intel_export_from_settings
from app.jobs.fetch_job import IntelFetchResult, run_intel_fetch_from_settings
from app.jobs.stage_a_screen_job import StageAScreenResult, run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import StageBAnalysisResult, run_stage_b_analysis_job
from app.jobs.stage_d_job import StageDResult, run_stage_d_from_settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelRun
from app.storage.repository import DAILY_DELTA_RUN_ITEM_ROLES, IntelCounts, IntelRepository, StageStateSummary


STAGE_ALIASES: dict[str, str] = {
    "fetch": "fetch",
    "a": "screen",
    "stage-a": "screen",
    "stage_a": "screen",
    "screen": "screen",
    "b": "analyze",
    "stage-b": "analyze",
    "stage_b": "analyze",
    "analyze": "analyze",
    "analysis": "analyze",
    "c": "cluster",
    "stage-c": "cluster",
    "stage_c": "cluster",
    "cluster": "cluster",
    "d": "stage_d",
    "stage-d": "stage_d",
    "stage_d": "stage_d",
    "export": "export",
}
DISPLAY_STAGE_NAMES = {
    "fetch": "fetch",
    "screen": "stage-a",
    "analyze": "stage-b",
    "cluster": "stage-c",
    "stage_d": "stage-d",
    "export": "export",
}
PIPELINE_STAGES = ("screen", "analyze", "cluster", "stage_d", "export")
PIPELINE_STAGE_ORDER = ("fetch", *PIPELINE_STAGES)
RETRYABLE_TASK_STATUSES = frozenset({"failed", "retry_waiting", "pending"})
STAGE_ACTIVE_TASK_STATUSES = frozenset({"pending", "running"})
STAGE_PARTIAL_TASK_STATUSES = frozenset({"failed", "retry_waiting", "blocked"})
STAGE_TERMINAL_TASK_STATUSES = frozenset({"succeeded", "skipped", "cancelled"})


@dataclass(frozen=True)
class PipelineStartResult:
    run_id: int
    fetch: IntelFetchResult
    reference_time: datetime | None = None
    scope_frozen: bool = True
    edition_date: str | None = None


@dataclass(frozen=True)
class PipelineStatus:
    run_id: int
    run_status: str
    reference_time: datetime | None
    scope_frozen: bool
    edition_date: str | None = None
    stages: tuple[dict[str, Any], ...] = ()
    total_failures: int = 0
    total_blocked: int = 0


@dataclass
class PipelineResumeResult:
    run_id: int
    ran_stages: list[str] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    skipped_stages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineExecutionResult:
    """Result of one complete run-scoped pipeline execution."""

    run_id: int
    start: PipelineStartResult
    resume: PipelineResumeResult
    status: str


@dataclass(frozen=True)
class PipelineAdoptResult:
    run_id: int
    adopted: dict[str, int]
    skipped: dict[str, int]


@dataclass(frozen=True)
class PipelineRunResult:
    """Compatibility result returned by the historical ``run-once`` facade."""

    run_id: int | None
    fetch: IntelFetchResult
    ai_review: AIReviewResult
    export: IntelExportResult
    status: str
    error: str | None = None
    event_cluster: EventClusterResult | None = None
    stage_d: StageDResult | None = None


def normalize_stage(value: str) -> str:
    key = str(value or "").strip().casefold()
    try:
        return STAGE_ALIASES[key]
    except KeyError as exc:
        allowed = ", ".join(sorted(set(STAGE_ALIASES.values())))
        raise ValueError(f"unknown pipeline stage {value!r}; expected one of: {allowed}") from exc


def _stage_c_current_event_ids(result: Any) -> Iterable[int] | None:
    """Expose Stage-C's complete current projection to Stage D."""

    current = getattr(result, "current_event_ids", None)
    if current is not None:
        return current
    # Injected cluster runners may expose only the historical new-event
    # projection; it remains a narrow fallback for old snapshots and tests.
    return getattr(result, "event_ids", None)


def _engine_and_factory(settings: Settings):
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return engine, create_session_factory(engine)


def _registry(settings: Settings, registry_path=DEFAULT_REGISTRY_PATH) -> dict[str, SourceSpec]:
    loaded = load_source_registry(
        registry_path,
        env={"RSSHUB_BASE_URL": settings.rsshub_base_url or ""},
    )
    return {source.id: source for source in loaded.sources}


def _stage_progress(repo: IntelRepository, stage: Any | None) -> str:
    """Classify a stage by its durable task progress.

    A stage with successful tasks plus retryable failures is *partial*, not
    active.  Its successful projections are safe inputs for downstream work;
    the failed tasks remain available to ``pipeline retry``.
    """

    if stage is None:
        return "missing"
    tasks = repo.list_stage_tasks(stage, include_expired=True)
    if not tasks:
        if str(stage.status) == "succeeded":
            return "succeeded"
        if str(stage.status) in {"failed", "blocked"}:
            return "partial"
        if str(stage.status) == "running":
            expires = stage.lease_expires_at
            if expires is None:
                return "active"
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            return "active" if expires > datetime.now(timezone.utc) else "partial"
        return "active"

    now = datetime.now(timezone.utc)
    for task in tasks:
        status = str(task.status)
        if status == "pending":
            return "active"
        if status == "running":
            expires = task.lease_expires_at
            if expires is None:
                return "active"
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires > now:
                return "active"
    if any(
        str(task.status) in STAGE_PARTIAL_TASK_STATUSES
        or (
            str(task.status) == "running"
            and task.lease_expires_at is not None
            and (
                task.lease_expires_at.replace(tzinfo=timezone.utc)
                if task.lease_expires_at.tzinfo is None
                else task.lease_expires_at
            ) <= now
        )
        for task in tasks
    ):
        return "partial"
    if all(str(task.status) in STAGE_TERMINAL_TASK_STATUSES for task in tasks):
        return "succeeded"
    return "active"


def _stage_has_successful_tasks(repo: IntelRepository, stage: Any | None) -> bool:
    """Return whether a stage has at least one reusable successful task."""

    return bool(stage is not None and repo.list_stage_tasks(stage, statuses={"succeeded"}))


def _sync_pipeline_run_status(
    session_factory: Any,
    run_id: int,
    *,
    finalize: bool = False,
    partial: bool | None = None,
    partial_reason: str | None = None,
) -> str | None:
    """Reconcile the run summary from all durable stage rows.

    Individual stage jobs own their task state, while this helper owns the
    aggregate ``IntelRun.status`` used by the CLI and exporters.  A run is
    only terminal after every fetch/pipeline stage is terminal; an active or
    not-yet-started downstream stage keeps the run open.
    """

    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None:
            return None

        stages = {stage.stage_name: stage for stage in repo.list_stages(int(run_id))}
        progress = {
            name: _stage_progress(repo, stages.get(name))
            for name in PIPELINE_STAGE_ORDER
        }

        if "missing" in progress.values() or "active" in progress.values():
            changed = False
            if run.status != "running" or run.finished_at is not None:
                run.status = "running"
                run.finished_at = None
                run.error = None
                changed = True
            if partial:
                if not run.partial:
                    run.partial = True
                    changed = True
                if partial_reason and run.partial_reason != partial_reason:
                    run.partial_reason = partial_reason
                    changed = True
            if changed:
                session.commit()
            return "running"

        partial_stages = [name for name, state in progress.items() if state == "partial"]
        if partial_stages:
            reason = partial_reason or "stage_partial:" + ",".join(partial_stages)
            repo.finish_run(
                int(run_id),
                status="partial",
                error=reason,
                partial=True,
                partial_reason=reason,
            )
            session.commit()
            return "partial"

        if all(state == "succeeded" for state in progress.values()):
            if not finalize:
                changed = False
                if run.status != "running" or run.finished_at is not None:
                    run.status = "running"
                    run.finished_at = None
                    run.error = None
                    changed = True
                if changed:
                    session.commit()
                return "running"
            # ``IntelRun.partial`` is a mutable summary, not immutable
            # provenance.  A same-run force rerun replaces the active stage
            # projections, so a previous ``ai_limit:*`` (or any completed
            # prior partial state) must not keep a now-complete run terminally
            # partial.  Current stage/task state above remains authoritative;
            # an explicit exporter partial still wins for this invocation.
            effective_partial = bool(partial) if partial is not None else False
            effective_reason = partial_reason if effective_partial else None
            target_status = "partial" if effective_partial else "completed"
            if (
                run.status != target_status
                or run.finished_at is None
                or bool(run.partial) != effective_partial
                or (run.partial_reason or None) != (effective_reason or None)
            ):
                repo.finish_run(
                    int(run_id),
                    status=target_status,
                    error=effective_reason if effective_partial else None,
                    partial=effective_partial,
                    partial_reason=effective_reason if effective_partial else "",
                )
                session.commit()
            return target_status

        # Unknown/non-terminal stage state: leave the run open and let the
        # next status/retry/resume operation reconcile it.
        return "running"


@contextmanager
def _stage_ai_client(settings: Settings, ai_client: Any | None = None):
    """Yield an injected provider or a short-lived configured provider."""

    if ai_client is not None:
        yield ai_client
        return
    client = httpx.Client(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        http2=True,
        trust_env=True,
        headers={"User-Agent": settings.user_agent},
    )
    try:
        yield IntelTriageClient.from_settings(settings, http_client=client)
    finally:
        client.close()


def _stage_d_client_or_none(value: Any | None) -> Any | None:
    """Do not accidentally pass a Stage-A/B triage client into Stage D."""

    if value is None:
        return None
    return value if any(callable(getattr(value, name, None)) for name in ("select_events", "stage_d_editorial", "editorial_select")) else None


def _stage_result_errors(stage_name: str, value: Any) -> list[str]:
    """Normalize explicit stage result errors for downstream safety guards.

    Stage D historically returned a result with ``errors`` after materializing
    a fallback snapshot.  Keep this compatibility check scoped to Stage D so
    the existing partial-success semantics of Stage A/B are unchanged.
    """

    if stage_name != "stage_d":
        return []
    errors = getattr(value, "errors", None)
    if not isinstance(errors, (list, tuple)):
        return []
    return [str(error) for error in errors if str(error).strip()]


def _freeze_after_fetch(
    settings: Settings,
    *,
    run_id: int,
    source: str | None,
    content_class: str | None,
    limit: int | None,
    fetch: IntelFetchResult,
) -> datetime | None:
    """Freeze a run's fetched membership and record its fetch stage summary."""

    _, session_factory = _engine_and_factory(settings)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} disappeared during fetch")
        # Freeze all fetched source lineage for audit, including unchanged
        # re-fetches. Stage A/B will separately consume only the daily delta.
        item_ids = repo.list_run_item_ids(run_id)
        source_ids = [source_id for source_id in (run.source_ids or []) if source_id]
        if not source_ids:
            stats = getattr(fetch, "stats", {}) or {}
            source_ids = list(dict.fromkeys(
                stat.source_id for stat in stats.values() if getattr(stat, "source_id", None)
            ))
        repo.freeze_run_scope(
            run_id,
            source_ids=source_ids,
            item_ids=item_ids,
            scope={
                "source": source,
                "content_class": content_class,
                "fetch_limit": limit,
            },
        )
        fetch_stage = repo.ensure_stage(
            run_id,
            "fetch",
            metadata={
                "source": source,
                "content_class": content_class,
                "items": len(item_ids),
            },
        )
        repo.finish_stage(
            fetch_stage,
            status="failed" if getattr(fetch, "total_failed", 0) else "succeeded",
            metadata={
                "fetched": getattr(fetch, "total_fetched", 0),
                "inserted": getattr(fetch, "total_inserted", 0),
                "failed": getattr(fetch, "total_failed", 0),
            },
        )
        session.commit()
        return run.reference_time


def start_pipeline_run_from_settings(
    *,
    settings: Settings,
    source: str | None = None,
    content_class: str | None = None,
    limit: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    force: bool = False,
    dry_run: bool = False,
    edition_date: date | str | None = None,
    registry_path=DEFAULT_REGISTRY_PATH,
    fetch_runner: Callable[..., IntelFetchResult] | None = None,
) -> PipelineStartResult:
    """Create/freeze one run and perform fetch only.

    The run is created before the fetch call, so every inserted item is
    attached to this immutable run scope.  No AI, cluster, Stage D or export
    function is called here.
    """

    if dry_run:
        # ``pipeline start --dry-run`` remains a diagnostic operation.  It
        # cannot produce a durable run id and therefore never claims to have
        # frozen a scope.
        runner = fetch_runner or run_intel_fetch_from_settings
        fetch = runner(
            settings=settings,
            registry_path=registry_path,
            limit_per_source=limit,
            source_filter=source,
            content_class=content_class,
            force=force,
            dry_run=True,
        )
        return PipelineStartResult(
            run_id=int(fetch.run_id or 0),
            fetch=fetch,
            reference_time=None,
            scope_frozen=False,
            edition_date=_normalize_edition_date(edition_date),
        )

    _, session_factory = _engine_and_factory(settings)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run(
            run_type="pipeline",
            filters={"source": source, "content_class": content_class, "stage": "fetch"},
            scope={
                "source": source,
                "content_class": content_class,
                "fetch_limit": limit,
                "reference_time": datetime.now(timezone.utc).isoformat(),
                **recent_window_scope(),
            },
            edition_date=edition_date,
        )
        session.commit()
        run_id = int(run.id)

    runner = fetch_runner or run_intel_fetch_from_settings
    fetch = runner(
        settings=settings,
        registry_path=registry_path,
        limit_per_source=limit,
        source_filter=source,
        content_class=content_class,
        force=force,
        dry_run=False,
        run_id=run_id,
    )

    # Freeze membership after fetch, even when one or more sources failed.
    # Failed source attempts remain auditable, while downstream stages see a
    # stable item set and cannot accidentally ingest later fetches.
    frozen_reference = _freeze_after_fetch(
        settings,
        run_id=run_id,
        source=source,
        content_class=content_class,
        limit=limit,
        fetch=fetch,
    )
    return PipelineStartResult(
        run_id=run_id,
        fetch=fetch,
        reference_time=frozen_reference,
        scope_frozen=True,
        edition_date=run.edition_date,
    )


def run_pipeline_stage_a_from_settings(
    *,
    settings: Settings,
    run_id: int,
    source: str | None = None,
    content_class: str | None = None,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    force: bool = False,
    retry_failed: bool = False,
    include_blocked: bool = False,
    item_ids: Iterable[int] | None = None,
    task_ids: Iterable[int] | None = None,
    ai_client: Any | None = None,
    registry_path=DEFAULT_REGISTRY_PATH,
) -> StageAScreenResult:
    _, session_factory = _engine_and_factory(settings)
    specs = _registry(settings, registry_path)
    with _stage_ai_client(settings, ai_client) as provider:
        result = run_stage_a_screen_job(
            session_factory=session_factory,
            source_specs=specs,
            ai_client=provider,
            run_id=int(run_id),
            limit=limit,
            source_filter=source,
            content_class=content_class,
            force=force,
            retry_failed=retry_failed,
            include_blocked=include_blocked,
            item_ids=item_ids,
            task_ids=task_ids,
            screen_reject_threshold=settings.ai_screen_reject_threshold,
            concurrency=settings.ai_review_concurrency,
        )
    _sync_pipeline_run_status(
        session_factory,
        int(run_id),
        finalize=False,
        partial=result.partial,
        partial_reason=result.partial_reason,
    )
    return result


def run_pipeline_stage_b_from_settings(
    *,
    settings: Settings,
    run_id: int,
    source: str | None = None,
    content_class: str | None = None,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    force: bool = False,
    retry_failed: bool = False,
    include_blocked: bool = False,
    item_ids: Iterable[int] | None = None,
    task_ids: Iterable[int] | None = None,
    ai_client: Any | None = None,
    registry_path=DEFAULT_REGISTRY_PATH,
) -> StageBAnalysisResult:
    _, session_factory = _engine_and_factory(settings)
    specs = _registry(settings, registry_path)
    with _stage_ai_client(settings, ai_client) as provider:
        result = run_stage_b_analysis_job(
            session_factory=session_factory,
            source_specs=specs,
            ai_client=provider,
            run_id=int(run_id),
            limit=limit,
            source_filter=source,
            content_class=content_class,
            force=force,
            retry_failed=retry_failed,
            include_blocked=include_blocked,
            item_ids=item_ids,
            task_ids=task_ids,
            analysis_min_score=settings.ai_analysis_min_score,
            concurrency=settings.ai_review_concurrency,
        )
    _sync_pipeline_run_status(
        session_factory,
        int(run_id),
        finalize=False,
        partial=result.partial,
        partial_reason=result.partial_reason,
    )
    return result


def run_pipeline_stage_c_from_settings(
    *,
    settings: Settings,
    run_id: int,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    force: bool = False,
    snapshot_key: str | None = None,
    ai_client: Any | None = None,
    item_ids: Iterable[int] | None = None,
) -> EventClusterResult:
    _, session_factory = _engine_and_factory(settings)
    result = run_event_cluster_from_settings(
        settings=settings,
        run_id=int(run_id),
        limit=limit,
        force=force,
        snapshot_key=snapshot_key or f"run-{int(run_id)}",
        item_ids=item_ids,
        ai_client=ai_client,
    )
    _sync_pipeline_run_status(session_factory, int(run_id), finalize=False)
    return result


def run_pipeline_stage_d_from_settings(
    *,
    settings: Settings,
    run_id: int,
    force: bool = False,
    snapshot_key: str | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    event_ids: Iterable[int] | None = None,
) -> StageDResult:
    _, session_factory = _engine_and_factory(settings)
    if ai_client is None:
        with httpx.Client(
            timeout=settings.ai_stage_d_timeout_seconds,
            follow_redirects=True,
            http2=True,
            trust_env=True,
            headers={"User-Agent": settings.user_agent},
        ) as http_client:
            result = run_stage_d_from_settings(
                settings=settings,
                run_id=int(run_id),
                force=force,
                snapshot_key=snapshot_key,
                profile_path=profile_path,
                ai_client=StageDEditorialClient.from_settings(settings, http_client=http_client),
                event_ids=event_ids,
            )
    else:
        result = run_stage_d_from_settings(
            settings=settings,
            run_id=int(run_id),
            force=force,
            snapshot_key=snapshot_key,
            profile_path=profile_path,
            ai_client=ai_client,
            event_ids=event_ids,
        )
    _sync_pipeline_run_status(session_factory, int(run_id), finalize=False)
    return result


def run_pipeline_export_from_settings(
    *,
    settings: Settings,
    run_id: int,
    limit: int | None = DEFAULT_DAILY_REPORT_LIMIT,
    source: str | None = None,
    content_class: str | None = None,
    output_dir: str | Path = "output/intel",
    dry_run: bool = False,
    snapshot_key: str | None = None,
    partial: bool = False,
    partial_reason: str | None = None,
) -> IntelExportResult:
    _, session_factory = _engine_and_factory(settings)
    result = run_intel_export_from_settings(
        settings=settings,
        run_id=int(run_id),
        limit=limit,
        source_filter=source,
        content_class=content_class,
        output_dir=output_dir,
        dry_run=dry_run,
        snapshot_key=snapshot_key,
        partial=partial,
        partial_reason=partial_reason,
    )
    _sync_pipeline_run_status(
        session_factory,
        int(run_id),
        finalize=True,
        partial=result.partial,
        partial_reason=result.partial_reason,
    )
    return result


def resolve_pipeline_run_id_from_settings(*, settings: Settings, edition_date: date | str) -> int:
    """Resolve a public daily date to its newest internal execution attempt."""

    normalized = _normalize_edition_date(edition_date)
    if normalized is None:
        raise ValueError("edition_date must use YYYY-MM-DD")
    _, session_factory = _engine_and_factory(settings)
    with session_factory() as session:
        run = IntelRepository(session).latest_run_for_edition(normalized)
        if run is None:
            raise ValueError(f"no pipeline run found for edition_date={normalized}")
        return int(run.id)


def pipeline_status_from_settings(*, settings: Settings, run_id: int) -> PipelineStatus:
    _, session_factory = _engine_and_factory(settings)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        existing = {stage.stage_name: repo.stage_summary(stage) for stage in repo.list_stages(run_id)}
        rows: list[dict[str, Any]] = []
        for name in ("fetch", *PIPELINE_STAGES):
            summary = existing.get(name)
            if summary is None:
                rows.append(
                    {
                        "stage": DISPLAY_STAGE_NAMES.get(name, name),
                        "stage_name": name,
                        "status": "pending",
                        "total": 0,
                        "pending": 0,
                        "running": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "retry_waiting": 0,
                        "blocked": 0,
                    }
                )
            else:
                value = _summary_dict(summary)
                value["stage"] = DISPLAY_STAGE_NAMES.get(name, name)
                rows.append(value)
        return PipelineStatus(
            run_id=int(run_id),
            run_status=str(run.status),
            reference_time=run.reference_time,
            scope_frozen=bool(run.scope_frozen),
            edition_date=run.edition_date,
            stages=tuple(rows),
            total_failures=sum(int(row.get("failed", 0)) for row in rows),
            total_blocked=sum(int(row.get("blocked", 0)) for row in rows),
        )


def adopt_existing_pipeline_from_settings(*, settings: Settings, run_id: int) -> PipelineAdoptResult:
    """Adopt only projection rows whose ``run_id`` matches exactly.

    This function intentionally never constructs an AI provider.  The storage
    repository performs the run-id check for each current projection and
    leaves mismatched rows absent rather than inventing historical state.
    """

    _, session_factory = _engine_and_factory(settings)
    adopted: dict[str, int] = {}
    skipped: dict[str, int] = {}
    with session_factory() as session:
        repo = IntelRepository(session)
        if session.get(IntelRun, int(run_id)) is None:
            raise ValueError(f"intel run {run_id} does not exist")
        for stage_name in ("screen", "analyze"):
            before = repo.get_stage(run_id, stage_name)
            previous = len(repo.list_stage_tasks(before)) if before is not None else 0
            rows = repo.adopt_existing_stage_tasks(run_id, stage_name)
            adopted[stage_name] = len(rows)
            skipped[stage_name] = max(
                0,
                previous + len(repo.list_run_item_ids(run_id, role=DAILY_DELTA_RUN_ITEM_ROLES)) - len(rows),
            )
        session.commit()
    return PipelineAdoptResult(run_id=int(run_id), adopted=adopted, skipped=skipped)


def retry_pipeline_stage_from_settings(
    *,
    settings: Settings,
    run_id: int,
    stage: str,
    include_blocked: bool = False,
    force: bool = False,
    source: str | None = None,
    content_class: str | None = None,
    limit: int | None = None,
    output_dir: str | Path = "output/intel",
    snapshot_key: str | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
) -> Any:
    """Reset only retryable tasks for one named stage, then run that stage."""

    canonical = normalize_stage(stage)
    if canonical == "fetch":
        raise ValueError("fetch is not a retryable pipeline stage; use pipeline start for a new fetch scope")
    _, session_factory = _engine_and_factory(settings)
    with session_factory() as session:
        repo = IntelRepository(session)
        if session.get(IntelRun, int(run_id)) is None:
            raise ValueError(f"intel run {run_id} does not exist")
        stage_row = repo.get_stage(run_id, canonical)
        if stage_row is None:
            return None
        repo.recover_expired_stage_tasks(stage_row)
        statuses = {"failed", "retry_waiting"}
        if include_blocked:
            statuses.add("blocked")
        tasks = repo.list_stage_tasks(stage_row, statuses=statuses, include_expired=True)
        task_ids = [int(task.id) for task in tasks]
        if not task_ids:
            return None
        repo.retry_failed(
            stage_row,
            include_blocked=include_blocked,
            task_ids=task_ids,
        )
        session.commit()

    if canonical == "screen":
        return run_pipeline_stage_a_from_settings(
            settings=settings,
            run_id=run_id,
            source=source,
            content_class=content_class,
            limit=limit if limit is not None else DEFAULT_AI_REVIEW_LIMIT,
            force=force,
            retry_failed=False,
            task_ids=task_ids,
            ai_client=ai_client,
        )
    if canonical == "analyze":
        return run_pipeline_stage_b_from_settings(
            settings=settings,
            run_id=run_id,
            source=source,
            content_class=content_class,
            limit=limit if limit is not None else DEFAULT_AI_REVIEW_LIMIT,
            force=force,
            retry_failed=False,
            task_ids=task_ids,
            ai_client=ai_client,
        )
    if canonical == "cluster":
        return run_pipeline_stage_c_from_settings(
            settings=settings,
            run_id=run_id,
            limit=limit,
            force=force,
            snapshot_key=snapshot_key,
            ai_client=ai_client,
        )
    if canonical == "stage_d":
        return run_pipeline_stage_d_from_settings(
            settings=settings,
            run_id=run_id,
            force=True,
            snapshot_key=snapshot_key,
            profile_path=profile_path,
            ai_client=_stage_d_client_or_none(ai_client),
        )
    return run_pipeline_export_from_settings(
        settings=settings,
        run_id=run_id,
        limit=limit if limit is not None else DEFAULT_DAILY_REPORT_LIMIT,
        output_dir=output_dir,
        snapshot_key=snapshot_key,
    )


def _stage_needs_resume(session_factory, run_id: int, stage_name: str) -> bool:
    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        stage = repo.get_stage(run_id, stage_name)
        if stage is not None:
            if stage.status in {"pending", "retry_waiting", "failed", "blocked"}:
                return True
            if repo.list_stage_tasks(stage, statuses=RETRYABLE_TASK_STATUSES, include_expired=True):
                return True
            if stage.status == "running":
                # An expired lease is made retryable by the next job claim;
                # a live running lease must not be duplicated.
                expires = stage.lease_expires_at
                if expires is None:
                    return True
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                return bool(expires <= datetime.now(timezone.utc))
            return False
        if stage_name == "screen":
            return bool(repo.list_run_item_ids(run_id, role=DAILY_DELTA_RUN_ITEM_ROLES))
        previous = {name: repo.get_stage(run_id, name) for name in PIPELINE_STAGES}
        dependency = {"analyze": "screen", "cluster": "analyze", "stage_d": "cluster", "export": "stage_d"}[stage_name]
        dependency_stage = previous[dependency]
        # Downstream stages may consume the successful subset while the
        # dependency still has retryable/blocked tasks.  Live pending/running
        # work still blocks the dependency, so a concurrent worker cannot race
        # a partially produced projection.
        dependency_progress = _stage_progress(repo, dependency_stage)
        # A fully successful empty stage is a legitimate daily outcome.  It
        # must still advance the remaining stages so an empty, auditable
        # export is produced instead of leaving the run permanently pending.
        if dependency_progress == "succeeded":
            return True
        return dependency_progress == "partial" and _stage_has_successful_tasks(repo, dependency_stage)


def resume_pipeline_from_settings(
    *,
    settings: Settings,
    run_id: int,
    fetch: bool = False,
    source: str | None = None,
    content_class: str | None = None,
    limit: int | None = None,
    output_dir: str | Path = "output/intel",
    snapshot_key: str | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
) -> PipelineResumeResult:
    """Resume eligible work in dependency order, never fetching by default."""

    _, session_factory = _engine_and_factory(settings)
    result = PipelineResumeResult(run_id=int(run_id))
    if fetch:
        # Explicit opt-in is kept for operators recovering an interrupted
        # fetch.  A frozen run still rejects genuinely new membership in the
        # repository, preserving the immutable scope contract.
        fetch_result = run_intel_fetch_from_settings(
            settings=settings,
            limit_per_source=limit if limit is not None else DEFAULT_FETCH_LIMIT_PER_SOURCE,
            source_filter=source,
            content_class=content_class,
            force=False,
            run_id=int(run_id),
        )
        result.ran_stages.append("fetch")
        result.results["fetch"] = fetch_result

    for stage_name in PIPELINE_STAGES:
        if not _stage_needs_resume(session_factory, run_id, stage_name):
            result.skipped_stages.append(stage_name)
            continue
        try:
            if stage_name == "screen":
                value = run_pipeline_stage_a_from_settings(
                    settings=settings,
                    run_id=run_id,
                    source=source,
                    content_class=content_class,
                    limit=limit if limit is not None else DEFAULT_AI_REVIEW_LIMIT,
                    ai_client=ai_client,
                )
            elif stage_name == "analyze":
                value = run_pipeline_stage_b_from_settings(
                    settings=settings,
                    run_id=run_id,
                    source=source,
                    content_class=content_class,
                    limit=limit if limit is not None else DEFAULT_AI_REVIEW_LIMIT,
                    ai_client=ai_client,
                )
            elif stage_name == "cluster":
                value = run_pipeline_stage_c_from_settings(
                    settings=settings,
                    run_id=run_id,
                    limit=limit,
                    snapshot_key=snapshot_key,
                    ai_client=ai_client,
                )
            elif stage_name == "stage_d":
                value = run_pipeline_stage_d_from_settings(
                    settings=settings,
                    run_id=run_id,
                    snapshot_key=snapshot_key,
                    profile_path=profile_path,
                    ai_client=_stage_d_client_or_none(ai_client),
                )
            else:
                value = run_pipeline_export_from_settings(
                    settings=settings,
                    run_id=run_id,
                    limit=limit if limit is not None else DEFAULT_DAILY_REPORT_LIMIT,
                    output_dir=output_dir,
                    snapshot_key=snapshot_key,
                )
            result.ran_stages.append(stage_name)
            result.results[stage_name] = value
            stage_errors = _stage_result_errors(stage_name, value)
            if stage_errors:
                result.errors.extend(f"{stage_name}: {error}" for error in stage_errors)
                break
        except Exception as exc:
            result.errors.append(f"{stage_name}: {exc}")
            break

    # Keep the run summary useful to operators without changing the frozen
    # item membership.  Downstream jobs own their stage/task projections, but
    # the orchestrator owns the aggregate terminal status.
    if result.errors:
        with session_factory() as session:
            repo = IntelRepository(session)
            if session.get(IntelRun, int(run_id)) is not None:
                repo.finish_run(run_id, status="failed", error="; ".join(result.errors))
                session.commit()
    else:
        _sync_pipeline_run_status(session_factory, int(run_id), finalize=True)
    return result


def run_pipeline_from_settings(
    *,
    settings: Settings,
    source: str | None = None,
    content_class: str | None = None,
    limit: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    force: bool = False,
    edition_date: date | str | None = None,
    output_dir: str | Path = "output/intel",
    snapshot_key: str | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    registry_path=DEFAULT_REGISTRY_PATH,
    on_start: Callable[[PipelineStartResult], None] | None = None,
) -> PipelineExecutionResult:
    """Create a run, fetch its immutable scope, then execute all stages.

    This is the normal operator entry point.  The stage-specific commands
    remain available for retries and recovery, but a successful invocation
    never requires the caller to copy a generated ``run_id`` between commands.
    ``limit`` applies to fetching per source; downstream stages process the
    complete frozen membership unless their dedicated retry command supplies a
    separate cap.
    """

    start = start_pipeline_run_from_settings(
        settings=settings,
        source=source,
        content_class=content_class,
        limit=limit,
        force=force,
        edition_date=edition_date,
        registry_path=registry_path,
    )
    if not start.scope_frozen or not start.run_id:
        raise ValueError("pipeline run requires a durable run; remove --dry-run from the start command")
    if on_start is not None:
        on_start(start)

    resumed = resume_pipeline_from_settings(
        settings=settings,
        run_id=int(start.run_id),
        source=source,
        content_class=content_class,
        # The fetch limit is per source.  Do not accidentally reuse it as a
        # global cap for the downstream stages.
        limit=None,
        output_dir=output_dir,
        snapshot_key=snapshot_key,
        profile_path=profile_path,
        ai_client=ai_client,
    )
    status = pipeline_status_from_settings(settings=settings, run_id=int(start.run_id)).run_status
    return PipelineExecutionResult(
        run_id=int(start.run_id),
        start=start,
        resume=resumed,
        status=status,
    )


def run_pipeline_once_from_settings(
    *,
    settings: Settings,
    source: str | None = None,
    content_class: str | None = None,
    limit: int = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    ai_limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    edition_date: date | str | None = None,
    output_dir: str | Path = "output/intel",
    profile_path: str | Path | None = None,
    snapshot_key: str | None = None,
    ai_client: Any | None = None,
    fetch_runner: Callable[..., IntelFetchResult] | None = None,
    ai_review_runner: Callable[..., AIReviewResult] | None = None,
    event_cluster_runner: Callable[..., EventClusterResult] | None = None,
    stage_d_runner: Callable[..., StageDResult] | None = None,
    export_runner: Callable[..., IntelExportResult] | None = None,
) -> PipelineRunResult:
    """Full convenience facade used by legacy ``run-once``."""

    fetch_fn = fetch_runner or run_intel_fetch_from_settings
    review_fn = ai_review_runner or run_ai_review_from_settings
    cluster_fn = event_cluster_runner or run_event_cluster_from_settings
    stage_d_fn = stage_d_runner or run_pipeline_stage_d_from_settings
    export_fn = export_runner or run_intel_export_from_settings

    if dry_run:
        effective_snapshot_key = snapshot_key or "latest"
        with TemporaryDirectory(prefix="intel-dry-run-") as temp_dir:
            ephemeral = Settings(**{**settings.__dict__, "database_url": f"sqlite:///{Path(temp_dir) / 'intel.db'}"})
            fetch = fetch_fn(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit_per_source=limit,
                force=force,
                dry_run=False,
            )
            review = review_fn(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit=ai_limit,
                ai_limit=ai_limit,
                force=force,
                dry_run=True,
                ai_client=ai_client,
            )
            cluster = cluster_fn(
                settings=ephemeral,
                limit=None,
                force=force,
                snapshot_key=effective_snapshot_key,
                run_id=review.run_id,
                item_ids=getattr(review, "candidate_ids", None),
            )
            stage_d = stage_d_fn(
                settings=ephemeral,
                profile_path=profile_path,
                force=force,
                snapshot_key=effective_snapshot_key,
                run_id=review.run_id,
                event_ids=_stage_c_current_event_ids(cluster),
                ai_client=_stage_d_client_or_none(ai_client),
            )
            stage_d_errors = _stage_result_errors("stage_d", stage_d)
            if stage_d_errors:
                raise RuntimeError("; ".join(stage_d_errors))
            exported = export_fn(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit=DEFAULT_DAILY_REPORT_LIMIT,
                output_dir=output_dir,
                dry_run=True,
                snapshot_key=effective_snapshot_key,
                partial=getattr(review, "partial", False),
                partial_reason=getattr(review, "partial_reason", None),
            )
        fetch = replace(fetch, dry_run=True)
        return PipelineRunResult(None, fetch, review, exported, "dry_run", None, cluster, stage_d)

    _, session_factory = _engine_and_factory(settings)
    with session_factory() as session:
        run = IntelRepository(session).start_run(
            run_type="run_once",
            filters={"source": source, "content_class": content_class, "stage": "run-once"},
            scope={
                "source": source,
                "content_class": content_class,
                "fetch_limit": limit,
                **recent_window_scope(),
            },
            edition_date=edition_date,
        )
        session.commit()
        run_id = int(run.id)

    # Event clustering keeps a run-scoped internal key.  The user-visible
    # Stage-D/export stages derive their default key from ``edition_date``.
    cluster_snapshot_key = snapshot_key or f"run-{run_id}"
    try:
        fetch = fetch_fn(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit_per_source=limit,
            force=force,
            run_id=run_id,
        )
        _freeze_after_fetch(
            settings,
            run_id=run_id,
            source=source,
            content_class=content_class,
            limit=limit,
            fetch=fetch,
        )
        review = review_fn(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit=ai_limit,
            ai_limit=ai_limit,
            force=force,
            run_id=run_id,
            ai_client=ai_client,
        )
        cluster = cluster_fn(
            settings=settings,
            limit=None,
            force=force,
            snapshot_key=cluster_snapshot_key,
            run_id=run_id,
            item_ids=getattr(review, "candidate_ids", None),
        )
        stage_d = stage_d_fn(
            settings=settings,
            profile_path=profile_path,
            force=force,
            snapshot_key=snapshot_key,
            run_id=run_id,
            event_ids=_stage_c_current_event_ids(cluster),
            ai_client=_stage_d_client_or_none(ai_client),
        )
        stage_d_errors = _stage_result_errors("stage_d", stage_d)
        if stage_d_errors:
            raise RuntimeError("; ".join(stage_d_errors))
        exported = export_fn(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit=DEFAULT_DAILY_REPORT_LIMIT,
            output_dir=output_dir,
            snapshot_key=snapshot_key,
            run_id=run_id,
            partial=getattr(review, "partial", False),
            partial_reason=getattr(review, "partial_reason", None),
        )
        status = "completed_with_errors" if (
            fetch.total_failed or getattr(review, "failed", 0) or getattr(cluster, "failed", 0)
        ) else "completed"
        with session_factory() as session:
            repo = IntelRepository(session)
            repo.finish_run(
                run_id,
                status=status,
                counts=IntelCounts(
                    fetched=fetch.total_fetched,
                    inserted=fetch.total_inserted,
                    skipped=fetch.total_skipped,
                    selected=getattr(review, "selected", 0),
                    screened=getattr(review, "screened", 0),
                    screened_out=getattr(review, "screened_out", 0),
                    screen_failed=getattr(review, "screen_failed", 0),
                    analyzed=getattr(review, "analyzed", 0),
                    analysis_filtered=getattr(review, "analysis_filtered", 0),
                    analysis_failed=getattr(review, "analysis_failed", 0),
                    candidate=getattr(review, "candidate", 0),
                    failed=fetch.total_failed + getattr(review, "failed", 0),
                    partial=int(getattr(review, "partial", False)),
                ),
                partial=getattr(review, "partial", False),
                partial_reason=getattr(review, "partial_reason", None),
            )
            session.commit()
        return PipelineRunResult(run_id, fetch, review, exported, status, None, cluster, stage_d)
    except Exception as exc:
        with session_factory() as session:
            IntelRepository(session).finish_run(run_id, status="failed", error=str(exc))
            session.commit()
        return PipelineRunResult(
            run_id,
            IntelFetchResult(run_id=run_id),
            AIReviewResult(run_id=run_id),
            IntelExportResult(
                0,
                f"{output_dir}/intel_items.jsonl",
                f"{output_dir}/intel_digest.md",
                run_id=run_id,
            ),
            "failed",
            str(exc),
            None,
            None,
        )


def _summary_dict(summary: StageStateSummary) -> dict[str, Any]:
    return {
        "stage_id": summary.stage_id,
        "run_id": summary.run_id,
        "stage_name": summary.stage_name,
        "status": summary.status,
        "total": summary.total,
        "pending": summary.pending,
        "running": summary.running,
        "succeeded": summary.succeeded,
        "failed": summary.failed,
        "retry_waiting": summary.retry_waiting,
        "blocked": summary.blocked,
    }


def _normalize_edition_date(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValueError("edition_date must use YYYY-MM-DD")
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("edition_date must use YYYY-MM-DD") from exc


# Short aliases make the orchestration layer convenient for non-CLI callers
# while the explicit ``*_from_settings`` names remain the dependency-injection
# seam used by the command handlers and tests.
start_pipeline_run = start_pipeline_run_from_settings
run_pipeline_stage_a = run_pipeline_stage_a_from_settings
run_pipeline_stage_b = run_pipeline_stage_b_from_settings
run_pipeline_stage_c = run_pipeline_stage_c_from_settings
run_pipeline_stage_d = run_pipeline_stage_d_from_settings
run_pipeline_export = run_pipeline_export_from_settings
pipeline_status = pipeline_status_from_settings
retry_pipeline_stage = retry_pipeline_stage_from_settings
resume_pipeline = resume_pipeline_from_settings
run_pipeline = run_pipeline_from_settings
adopt_existing = adopt_existing_pipeline_from_settings


__all__ = [
    "DISPLAY_STAGE_NAMES",
    "PIPELINE_STAGES",
    "PipelineAdoptResult",
    "PipelineExecutionResult",
    "PipelineResumeResult",
    "PipelineRunResult",
    "PipelineStartResult",
    "PipelineStatus",
    "adopt_existing_pipeline_from_settings",
    "adopt_existing",
    "normalize_stage",
    "pipeline_status_from_settings",
    "pipeline_status",
    "resolve_pipeline_run_id_from_settings",
    "resume_pipeline_from_settings",
    "resume_pipeline",
    "run_pipeline_from_settings",
    "run_pipeline",
    "retry_pipeline_stage_from_settings",
    "retry_pipeline_stage",
    "run_pipeline_export_from_settings",
    "run_pipeline_export",
    "run_pipeline_once_from_settings",
    "run_pipeline_stage_a_from_settings",
    "run_pipeline_stage_a",
    "run_pipeline_stage_b_from_settings",
    "run_pipeline_stage_b",
    "run_pipeline_stage_c_from_settings",
    "run_pipeline_stage_c",
    "run_pipeline_stage_d_from_settings",
    "run_pipeline_stage_d",
    "start_pipeline_run_from_settings",
    "start_pipeline_run",
]
