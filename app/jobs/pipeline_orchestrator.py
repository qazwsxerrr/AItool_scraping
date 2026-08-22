"""Date-addressed orchestration for the resumable intelligence pipeline.

The individual jobs own their stage semantics.  This module only creates and
creates a private build scope, dispatches one named stage at a time, and
exposes the small control-plane operations used by the CLI (status, retry and
resume).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
import logging
from pathlib import Path
import shutil
from typing import Any, Callable, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.ai.skills.intel_triage import IntelTriageClient
from app.ai.skills.stage_d_selection import MIN_STAGE_D_TIMEOUT_SECONDS, StageDSelectionClient
from app.config.limits import (
    DEFAULT_AI_REVIEW_LIMIT,
    DEFAULT_DAILY_REPORT_LIMIT,
    DEFAULT_FETCH_LIMIT_PER_SOURCE,
    STAGE_C_AGENT_VERSION,
)
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, load_source_registry
from app.domain.models import SourceSpec
from app.domain.recency import recent_window_scope
from app.jobs.event_cluster_job import EventClusterResult, run_event_cluster_from_settings
from app.jobs.export_job import (
    IntelExportResult,
    create_daily_bundle_staging_dir,
    daily_output_dir_for_run,
    finalize_daily_bundle,
    promote_daily_bundle,
    rollback_daily_bundle,
    run_intel_export_from_settings,
)
from app.jobs.fetch_job import IntelFetchResult, run_intel_fetch_from_settings
from app.jobs.stage_a_screen_job import StageAScreenResult, run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import StageBAnalysisResult, run_stage_b_analysis_job
from app.jobs.stage_d_job import STAGE_D_VERSION, StageDResult, run_stage_d_from_settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import DailyEdition, IntelRun
from app.storage.draft_workspace import (
    audit_database_path,
    audit_settings,
    create_daily_draft,
    daily_audit_exists,
    daily_draft_exists,
    draft_settings,
    finalize_daily_audit,
    normalize_draft_edition_date,
    promote_daily_draft_to_audit,
    rollback_daily_audit,
)
from app.storage.repository import DAILY_EDITION_TIMEZONE, IntelRepository, StageStateSummary


logger = logging.getLogger(__name__)


PUBLIC_STAGE_NAMES: dict[str, str] = {
    "fetch": "fetch",
    "stage-a": "screen",
    "stage-b1": "analyze",
    "stage-c": "cluster",
    "stage-d": "stage_d",
}
DISPLAY_STAGE_NAMES = {
    "fetch": "fetch",
    "screen": "stage-a",
    "analyze": "stage-b1",
    "cluster": "stage-c",
    "stage_d": "stage-d",
    "export": "export",
}
PIPELINE_STAGES = ("screen", "analyze", "cluster", "stage_d")
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


@dataclass(frozen=True)
class DailyEditionStatus:
    """Public status for one date-addressed report workspace."""

    edition_date: str
    status: str
    published_at: datetime | None = None
    draft_status: str | None = None
    audit_status: str | None = None
    audit_path: str | None = None
    stages: tuple[dict[str, Any], ...] = ()
    total_failures: int = 0
    total_blocked: int = 0
    error: str | None = None


@dataclass
class PipelineResumeResult:
    run_id: int
    ran_stages: list[str] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    skipped_stages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineExecutionResult:
    """Result of one complete date-addressed pipeline execution."""

    run_id: int
    start: PipelineStartResult
    resume: PipelineResumeResult
    status: str


def normalize_stage(value: str) -> str:
    key = str(value or "").strip().casefold()
    if key in PIPELINE_STAGE_ORDER:
        return key
    try:
        return PUBLIC_STAGE_NAMES[key]
    except KeyError as exc:
        allowed = ", ".join((*PUBLIC_STAGE_NAMES, "screen", "analyze", "cluster", "stage_d"))
        raise ValueError(f"unknown pipeline stage {value!r}; expected one of: {allowed}") from exc


def _engine_and_factory(settings: Settings):
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return engine, create_session_factory(engine)


def _resolved_edition_date(value: date | str | None) -> str:
    normalized = _normalize_edition_date(value)
    if normalized is not None:
        return normalized
    return datetime.now(timezone.utc).astimezone(DAILY_EDITION_TIMEZONE).date().isoformat()


def _published_edition_from_settings(settings: Settings, edition_date: str) -> tuple[datetime | None, str | None]:
    """Read one public report without initializing or mutating its database."""

    engine = create_engine_from_url(settings.database_url)
    try:
        session_factory = create_session_factory(engine)
        with session_factory() as session:
            edition = session.scalar(
                select(DailyEdition).where(
                    DailyEdition.edition_date == date.fromisoformat(edition_date),
                    DailyEdition.published_at.is_not(None),
                )
            )
            if edition is None:
                return None, None
            return edition.published_at, str(edition.status)
    except OperationalError:
        # A brand-new installation has no public report database yet.
        return None, None
    finally:
        engine.dispose()


def draft_settings_for_edition(*, settings: Settings, edition_date: date | str) -> Settings:
    """Return the private database settings for one date-addressed draft."""

    return draft_settings(settings, normalize_draft_edition_date(edition_date))


def audit_settings_for_edition(*, settings: Settings, edition_date: date | str) -> Settings:
    """Return settings for one date's retained, published-build audit."""

    return audit_settings(settings, normalize_draft_edition_date(edition_date))


def resolve_pending_daily_draft_from_settings(
    *,
    settings: Settings,
    edition_date: date | str,
) -> tuple[Settings, int]:
    """Resolve the isolated draft DB and its opaque build ID for a date."""

    normalized = normalize_draft_edition_date(edition_date)
    if not daily_draft_exists(settings, normalized):
        raise ValueError(
            f"no pending draft for edition_date={normalized}; run pipeline start or pipeline run first"
        )
    workspace_settings = draft_settings_for_edition(settings=settings, edition_date=normalized)
    engine, session_factory = _engine_and_factory(workspace_settings)
    try:
        with session_factory() as session:
            run = IntelRepository(session).draft_run_for_edition(normalized)
            if run is None:
                raise ValueError(
                    f"no pending draft for edition_date={normalized}; run pipeline start or pipeline run first"
                )
            return workspace_settings, int(run.id)
    finally:
        engine.dispose()


def _retained_daily_audit_from_settings(
    *,
    settings: Settings,
    edition_date: str,
) -> tuple[Settings, int] | None:
    """Resolve the retained audit build without treating it as a draft."""

    if not daily_audit_exists(settings, edition_date):
        return None
    workspace_settings = audit_settings_for_edition(settings=settings, edition_date=edition_date)
    engine, session_factory = _engine_and_factory(workspace_settings)
    try:
        with session_factory() as session:
            run = IntelRepository(session).draft_run_for_edition(edition_date)
            if run is None:
                return None
            return workspace_settings, int(run.id)
    finally:
        engine.dispose()


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
            reason = partial_reason or run.partial_reason or "stage_partial:" + ",".join(partial_stages)
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


def _daily_stage_result_errors(stage_name: str, value: Any) -> list[str]:
    """Failures that must block publication of a complete daily rebuild."""

    errors: list[str] = []
    explicit = getattr(value, "errors", None)
    if isinstance(explicit, (list, tuple)):
        errors.extend(str(error) for error in explicit if str(error).strip() and str(error) not in errors)
    if bool(getattr(value, "partial", False)):
        errors.append(str(getattr(value, "partial_reason", None) or f"{stage_name}_partial"))
    for attribute in ("screen_failed", "analysis_failed", "failed", "ai_failed"):
        try:
            count = int(getattr(value, attribute, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        if count:
            errors.append(f"{stage_name}:{attribute}={count}")
    return list(dict.fromkeys(errors))


def _freeze_after_fetch(
    settings: Settings,
    *,
    run_id: int,
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
        # A DailyEdition build is a full replacement build. Freeze every
        # fetched item; Stage A consumes the complete draft scope rather than
        # a global incremental delta.
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
                "fetch_limit": limit,
            },
        )
        fetch_stage = repo.ensure_stage(
            run_id,
            "fetch",
            metadata={
                "items": len(item_ids),
                "sources": len(source_ids),
            },
        )
        repo.finish_stage(
            fetch_stage,
            status="failed" if getattr(fetch, "total_failed", 0) else "succeeded",
            metadata={
                "sources": len(source_ids),
                "fetched": getattr(fetch, "total_fetched", 0),
                "inserted": getattr(fetch, "total_inserted", 0),
                "failed": getattr(fetch, "total_failed", 0),
            },
        )
        if getattr(fetch, "total_failed", 0) and run.edition_id is not None:
            error = f"fetch_failed_sources:{int(getattr(fetch, 'total_failed', 0))}"
            # Source failures are isolated to their own attempts. Freeze the
            # successful source set and let A-D build a non-public partial
            # draft from it; only a complete source set may replace the daily
            # report.
            run.partial = True
            run.partial_reason = error
            run.error = error
            edition = session.get(DailyEdition, int(run.edition_id))
            if edition is not None:
                edition.status = "building_with_errors"
                edition.error = error
        session.commit()
        return run.reference_time


def _mark_daily_build_failed(session_factory: Any, run_id: int, error: str | None) -> None:
    """Persist a failed draft state without replacing its public report."""

    with session_factory() as session:
        repo = IntelRepository(session)
        if session.get(IntelRun, int(run_id)) is not None:
            repo.finish_run(int(run_id), status="failed", error=error)
            repo.mark_daily_build_failed(int(run_id), error=error)
            session.commit()


def _remap_staged_artifact_path(
    value: str | None,
    *,
    staging_dir: str | Path,
    final_dir: str | Path,
) -> str | None:
    if not value:
        return None
    try:
        relative = Path(value).relative_to(Path(staging_dir))
    except ValueError:
        return value
    return str(Path(final_dir) / relative)


def start_pipeline_run_from_settings(
    *,
    settings: Settings,
    limit: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    edition_date: date | str | None = None,
    registry_path=DEFAULT_REGISTRY_PATH,
    fetch_runner: Callable[..., IntelFetchResult] | None = None,
) -> PipelineStartResult:
    """Create a fresh, fully isolated draft for one public edition date."""

    normalized_edition = _resolved_edition_date(edition_date)
    # Starting again discards only the prior pending build for this date.  A
    # retained audit from the last approved build, the published database and
    # its old report all remain untouched until this new draft is approved.
    workspace_settings = create_daily_draft(settings, normalized_edition)

    _, session_factory = _engine_and_factory(workspace_settings)
    with session_factory() as session:
        repo = IntelRepository(session)
        edition, run = repo.start_daily_build(
            edition_date=normalized_edition,
            filters={"stage": "fetch"},
            scope={
                "fetch_limit": limit,
                "reference_time": datetime.now(timezone.utc).isoformat(),
                **recent_window_scope(edition_date=normalized_edition),
            },
        )
        session.commit()
        run_id = int(run.id)

    runner = fetch_runner or run_intel_fetch_from_settings
    fetch = runner(
        settings=workspace_settings,
        registry_path=registry_path,
        limit_per_source=limit,
        source_filter=None,
        content_class=None,
        force=True,
        dry_run=False,
        run_id=run_id,
    )

    # Freeze membership after fetch, even when one or more sources failed.
    # Failed source attempts remain auditable, while downstream stages see a
    # stable item set and cannot accidentally ingest later fetches.
    frozen_reference = _freeze_after_fetch(
        workspace_settings,
        run_id=run_id,
        limit=limit,
        fetch=fetch,
    )
    return PipelineStartResult(
        run_id=run_id,
        fetch=fetch,
        reference_time=frozen_reference,
        scope_frozen=True,
        edition_date=edition.edition_date.isoformat(),
    )


def run_pipeline_stage_a_from_settings(
    *,
    settings: Settings,
    run_id: int,
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
            source_filter=None,
            content_class=None,
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
            source_filter=None,
            content_class=None,
            force=force,
            retry_failed=retry_failed,
            include_blocked=include_blocked,
            item_ids=item_ids,
            task_ids=task_ids,
            analysis_min_score=settings.ai_analysis_min_score,
            reserve_limit=settings.stage_b_reserve_limit,
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
    force: bool = False,
    ai_client: Any | None = None,
) -> EventClusterResult:
    _, session_factory = _engine_and_factory(settings)
    result = run_event_cluster_from_settings(
        settings=settings,
        run_id=int(run_id),
        force=force,
        ai_client=ai_client,
    )
    _sync_pipeline_run_status(session_factory, int(run_id), finalize=False)
    return result


def run_pipeline_stage_d_from_settings(
    *,
    settings: Settings,
    run_id: int,
    force: bool = False,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
) -> StageDResult:
    _, session_factory = _engine_and_factory(settings)
    if ai_client is None:
        with httpx.Client(
            timeout=max(float(settings.ai_review_timeout_seconds), MIN_STAGE_D_TIMEOUT_SECONDS),
            follow_redirects=True,
            http2=True,
            trust_env=True,
            headers={"User-Agent": settings.user_agent},
        ) as http_client:
            result = run_stage_d_from_settings(
                settings=settings,
                run_id=int(run_id),
                force=force,
                profile_path=profile_path,
                ai_client=StageDSelectionClient.from_settings(settings, http_client=http_client),
            )
    else:
        result = run_stage_d_from_settings(
            settings=settings,
            run_id=int(run_id),
            force=force,
            profile_path=profile_path,
            ai_client=ai_client,
        )
    _sync_pipeline_run_status(session_factory, int(run_id), finalize=False)
    return result


def publish_daily_draft_from_settings(
    *,
    settings: Settings,
    edition_date: date | str,
    limit: int | None = DEFAULT_DAILY_REPORT_LIMIT,
    output_dir: str | Path = "output/intel",
) -> IntelExportResult:
    """Approve one draft and atomically replace its public report.

    Export rendering and every stage row remain in the date workspace.  The
    pending ``draft.db`` becomes the retained ``audit.db`` only as part of the
    same success path that replaces the public bundle and final report.  The
    published database is touched only inside the final replacement
    transaction, after the draft is complete and its bundle has been staged.
    """

    normalized = normalize_draft_edition_date(edition_date)
    workspace_settings, run_id = resolve_pending_daily_draft_from_settings(
        settings=settings,
        edition_date=normalized,
    )
    draft_engine, draft_factory = _engine_and_factory(workspace_settings)
    try:
        with draft_factory() as session:
            run = session.get(IntelRun, int(run_id))
            if run is None:
                raise ValueError(f"draft build disappeared for edition_date={normalized}")
            if run.edition_date != normalized:
                raise ValueError("draft build does not match the requested edition_date")
            final_dir = daily_output_dir_for_run(output_dir, run)
            staging_dir = create_daily_bundle_staging_dir(output_dir, run)
    except Exception:
        draft_engine.dispose()
        raise

    published = False
    bundle_promotion = None
    audit_promotion = None
    try:
        run_status = _sync_pipeline_run_status(draft_factory, int(run_id), finalize=True)
        if run_status != "completed":
            with draft_factory() as session:
                repo = IntelRepository(session)
                if run_status == "partial":
                    repo.mark_daily_build_partial(int(run_id), error="daily build is partial and cannot be published")
                else:
                    repo.mark_daily_build_failed(
                        int(run_id),
                        error=f"daily build is not publishable: {run_status or 'unknown'}",
                    )
                session.commit()
            raise RuntimeError(f"daily build is not publishable: {run_status or 'unknown'}")

        result = run_intel_export_from_settings(
            settings=workspace_settings,
            run_id=int(run_id),
            limit=limit,
            output_dir=output_dir,
            dry_run=False,
            artifact_dir=staging_dir,
        )
        if result.partial:
            raise RuntimeError(f"daily build is not publishable: {result.partial_reason or 'partial'}")

        # Close this process's connection pool before moving SQLite files.
        # The exporter has completed all writes at this point, so the retained
        # audit is a complete snapshot of raw fetches and every A-D decision.
        candidate_count = _draft_stage_d_candidate_count(draft_factory, int(run_id))
        draft_engine.dispose()
        audit_promotion = promote_daily_draft_to_audit(settings, normalized)
        bundle_promotion = promote_daily_bundle(staging_dir=staging_dir, final_dir=final_dir)
        public_engine = create_engine_from_url(settings.database_url)
        try:
            # This initialization only creates missing tables; it never starts
            # a build or marks a published edition as rebuilding.
            init_db(public_engine)
            public_factory = create_session_factory(public_engine)
            with public_factory() as session:
                IntelRepository(session).replace_published_daily_report(
                    edition_date=normalized,
                    records=result.records,
                    candidate_count=candidate_count,
                )
                session.commit()
        finally:
            public_engine.dispose()
        published = True
        try:
            finalize_daily_bundle(bundle_promotion)
            finalize_daily_audit(audit_promotion)
        except OSError:
            # The public report is already durable.  Leave a clearly named
            # rollback artifact for an operator instead of misreporting the
            # publication as failed or rolling back only part of it.
            logger.warning("published daily audit backup cleanup failed", exc_info=True)
        return replace(
            result,
            jsonl_path=str(final_dir / "intel_items.jsonl"),
            markdown_path=str(final_dir / "intel_digest.md"),
            manifest_path=str(final_dir / "manifest.json"),
            github_report_path=_remap_staged_artifact_path(
                result.github_report_path,
                staging_dir=staging_dir,
                final_dir=final_dir,
            ),
        )
    except Exception as exc:
        if not published:
            if bundle_promotion is not None:
                rollback_daily_bundle(bundle_promotion)
            if audit_promotion is not None:
                rollback_daily_audit(audit_promotion)
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            _mark_daily_build_failed(draft_factory, int(run_id), str(exc))
        raise
    finally:
        draft_engine.dispose()


def pipeline_edition_status_from_settings(
    *,
    settings: Settings,
    edition_date: date | str,
) -> DailyEditionStatus:
    """Return public, pending-draft and retained-audit state by date."""

    normalized = normalize_draft_edition_date(edition_date)
    published_at, _ = _published_edition_from_settings(settings, normalized)
    retained_audit = _retained_daily_audit_from_settings(
        settings=settings,
        edition_date=normalized,
    )
    audit_path = str(audit_database_path(settings.database_url, normalized)) if retained_audit else None
    audit_status = "retained" if retained_audit else None
    audit_run_status = None
    if retained_audit is not None:
        audit_settings_value, audit_run_id = retained_audit
        audit_run_status = pipeline_status_from_settings(
            settings=audit_settings_value,
            run_id=audit_run_id,
        )

    if not daily_draft_exists(settings, normalized):
        if published_at is None and audit_run_status is None:
            raise ValueError(f"no daily edition, draft, or audit found for edition_date={normalized}")
        if published_at is None:
            return DailyEditionStatus(
                edition_date=normalized,
                status="audit_retained",
                audit_status=audit_status,
                audit_path=audit_path,
                stages=audit_run_status.stages if audit_run_status is not None else (),
                total_failures=audit_run_status.total_failures if audit_run_status is not None else 0,
                total_blocked=audit_run_status.total_blocked if audit_run_status is not None else 0,
            )
        return DailyEditionStatus(
            edition_date=normalized,
            status="published",
            published_at=published_at,
            audit_status=audit_status,
            audit_path=audit_path,
            stages=audit_run_status.stages if audit_run_status is not None else (),
            total_failures=audit_run_status.total_failures if audit_run_status is not None else 0,
            total_blocked=audit_run_status.total_blocked if audit_run_status is not None else 0,
        )

    workspace_settings, run_id = resolve_pending_daily_draft_from_settings(
        settings=settings,
        edition_date=normalized,
    )
    run_status = pipeline_status_from_settings(settings=workspace_settings, run_id=run_id)
    draft_state = str(run_status.run_status)
    if draft_state == "completed":
        draft_state = "ready_for_publish"
    public_state = "published" if published_at is not None else draft_state
    error: str | None = None
    draft_engine, session_factory = _engine_and_factory(workspace_settings)
    try:
        with session_factory() as session:
            run = session.get(IntelRun, int(run_id))
            if run is not None:
                error = run.error or run.partial_reason
    finally:
        draft_engine.dispose()
    return DailyEditionStatus(
        edition_date=normalized,
        status=public_state,
        published_at=published_at,
        draft_status=draft_state,
        audit_status=audit_status,
        audit_path=audit_path,
        stages=run_status.stages,
        total_failures=run_status.total_failures,
        total_blocked=run_status.total_blocked,
        error=error,
    )


def pipeline_status_from_settings(*, settings: Settings, run_id: int) -> PipelineStatus:
    engine, session_factory = _engine_and_factory(settings)
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id))
            if run is None:
                raise ValueError(f"intel run {run_id} does not exist")
            stages = {stage.stage_name: stage for stage in repo.list_stages(run_id)}
            existing = {name: repo.stage_summary(stage) for name, stage in stages.items()}
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
                    # Fetch uses durable source attempts rather than generic
                    # stage-task rows. Surface that source-level outcome instead
                    # of rendering a failed fetch as ``failed=0``.
                    if name == "fetch" and value["total"] == 0:
                        metadata = stages[name].metadata_dict
                        total = max(
                            0,
                            int(metadata.get("sources") or len(run.source_ids or ())),
                        )
                        failed = max(0, int(metadata.get("failed") or 0))
                        value.update(
                            total=total,
                            succeeded=max(0, total - failed),
                            failed=failed,
                        )
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
    finally:
        engine.dispose()


def retry_pipeline_stage_from_settings(
    *,
    settings: Settings,
    run_id: int,
    stage: str,
    include_blocked: bool = False,
    force: bool = False,
    limit: int | None = None,
    output_dir: str | Path = "output/intel",
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
        run = session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        if run.edition_id is None:
            raise ValueError("pipeline retry requires the current daily edition build")
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
            force=force,
            ai_client=ai_client,
        )
    if canonical == "stage_d":
        return run_pipeline_stage_d_from_settings(
            settings=settings,
            run_id=run_id,
            force=True,
            profile_path=profile_path,
            ai_client=ai_client,
        )
    raise ValueError("export is an approval action; use pipeline export --edition-date instead")


def _stage_needs_resume(
    session_factory,
    run_id: int,
    stage_name: str,
    *,
    settings: Settings | None = None,
) -> bool:
    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        stage = repo.get_stage(run_id, stage_name)
        if stage is not None:
            if stage.status == "succeeded":
                metadata = stage.metadata_dict
                if stage_name == "cluster":
                    if metadata.get("agent_version") != STAGE_C_AGENT_VERSION:
                        return True
                if stage_name == "stage_d" and str(metadata.get("stage_d_version") or "") != STAGE_D_VERSION:
                    return True
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
            return bool(repo.list_run_item_ids(run_id, role=None))
        previous = {name: repo.get_stage(run_id, name) for name in PIPELINE_STAGES}
        dependency = {"analyze": "screen", "cluster": "analyze", "stage_d": "cluster"}[stage_name]
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
    limit: int | None = None,
    output_dir: str | Path = "output/intel",
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
) -> PipelineResumeResult:
    """Resume the one pending daily build in dependency order."""

    _, session_factory = _engine_and_factory(settings)
    result = PipelineResumeResult(run_id=int(run_id))
    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        if run.edition_id is None:
            raise ValueError("pipeline resume requires the current daily edition build")
        fetch_stage = repo.get_stage(int(run_id), "fetch")
        if fetch_stage is None or fetch_stage.status not in {"succeeded", "failed"}:
            reason = "fetch stage is incomplete; start a fresh full pipeline run for this edition"
            repo.mark_daily_build_failed(int(run_id), error=reason)
            session.commit()
            result.errors.append(reason)
            return result
        if fetch_stage.status == "failed" and not repo.list_run_item_ids(int(run_id)):
            reason = "fetch stage produced no usable items; start a fresh full pipeline run for this edition"
            repo.mark_daily_build_failed(int(run_id), error=reason)
            session.commit()
            result.errors.append(reason)
            return result

    for stage_name in PIPELINE_STAGES:
        if not _stage_needs_resume(session_factory, run_id, stage_name, settings=settings):
            result.skipped_stages.append(stage_name)
            continue
        try:
            if stage_name == "screen":
                value = run_pipeline_stage_a_from_settings(
                    settings=settings,
                    run_id=run_id,
                    limit=limit if limit is not None else DEFAULT_AI_REVIEW_LIMIT,
                    ai_client=ai_client,
                )
            elif stage_name == "analyze":
                value = run_pipeline_stage_b_from_settings(
                    settings=settings,
                    run_id=run_id,
                    limit=limit if limit is not None else DEFAULT_AI_REVIEW_LIMIT,
                    ai_client=ai_client,
                )
            elif stage_name == "cluster":
                value = run_pipeline_stage_c_from_settings(
                    settings=settings,
                    run_id=run_id,
                    ai_client=ai_client,
                )
            elif stage_name == "stage_d":
                value = run_pipeline_stage_d_from_settings(
                    settings=settings,
                    run_id=run_id,
                    profile_path=profile_path,
                    ai_client=ai_client,
                )
            else:  # pragma: no cover - PIPELINE_STAGES is exhaustive.
                raise RuntimeError(f"unsupported pipeline stage: {stage_name}")
            result.ran_stages.append(stage_name)
            result.results[stage_name] = value
            stage_errors = _daily_stage_result_errors(stage_name, value)
            if stage_errors:
                result.errors.extend(f"{stage_name}: {error}" for error in stage_errors)
                _mark_daily_build_failed(session_factory, int(run_id), "; ".join(result.errors))
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
                repo.mark_daily_build_failed(run_id, error="; ".join(result.errors))
                session.commit()
    else:
        _sync_pipeline_run_status(session_factory, int(run_id), finalize=True)
    return result


def run_pipeline_from_settings(
    *,
    settings: Settings,
    limit: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    edition_date: date | str | None = None,
    output_dir: str | Path = "output/intel",
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    registry_path=DEFAULT_REGISTRY_PATH,
    on_start: Callable[[PipelineStartResult], None] | None = None,
    publish: bool = False,
) -> PipelineExecutionResult:
    """Fully rebuild one date-addressed draft, optionally publishing it."""

    start = start_pipeline_run_from_settings(
        settings=settings,
        limit=limit,
        edition_date=edition_date,
        registry_path=registry_path,
    )
    if not start.scope_frozen:
        raise RuntimeError("daily build did not freeze its fetched scope")
    if on_start is not None:
        on_start(start)

    resolved_date = start.edition_date or _normalize_edition_date(edition_date)
    if resolved_date is None:
        raise RuntimeError("daily build did not resolve an edition date")
    workspace_settings = draft_settings_for_edition(settings=settings, edition_date=resolved_date)
    resumed = resume_pipeline_from_settings(
        settings=workspace_settings,
        run_id=int(start.run_id),
        # The fetch limit is per source.  Do not accidentally reuse it as a
        # global cap for the downstream stages.
        limit=None,
        output_dir=output_dir,
        profile_path=profile_path,
        ai_client=ai_client,
    )
    if publish and not resumed.errors:
        draft_run_status = pipeline_status_from_settings(
            settings=workspace_settings,
            run_id=int(start.run_id),
        ).run_status
        if draft_run_status == "completed":
            resumed.results["export"] = publish_daily_draft_from_settings(
                settings=settings,
                edition_date=resolved_date,
                limit=DEFAULT_DAILY_REPORT_LIMIT,
                output_dir=output_dir,
            )
            resumed.ran_stages.append("export")
    status = pipeline_edition_status_from_settings(settings=settings, edition_date=resolved_date).status
    return PipelineExecutionResult(
        run_id=int(start.run_id),
        start=start,
        resume=resumed,
        status=status,
    )
def _summary_dict(summary: StageStateSummary) -> dict[str, Any]:
    return {
        "stage_id": summary.stage_id,
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


def _draft_stage_d_candidate_count(session_factory, run_id: int) -> int:
    """Read the final C candidate pool size before the draft is promoted."""

    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(int(run_id), "stage_d")
        task = repo.get_task(stage, subject_type="run", subject_id=int(run_id)) if stage else None
        result = task.result if task is not None and isinstance(task.result, dict) else {}
        candidate_ids = result.get("candidate_event_ids") if isinstance(result, dict) else None
        return len(candidate_ids) if isinstance(candidate_ids, list) else 0


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


__all__ = [
    "DISPLAY_STAGE_NAMES",
    "PIPELINE_STAGES",
    "PipelineExecutionResult",
    "PipelineResumeResult",
    "PipelineStartResult",
    "PipelineStatus",
    "draft_settings_for_edition",
    "normalize_stage",
    "publish_daily_draft_from_settings",
    "pipeline_status_from_settings",
    "pipeline_edition_status_from_settings",
    "resolve_pending_daily_draft_from_settings",
    "resume_pipeline_from_settings",
    "run_pipeline_from_settings",
    "retry_pipeline_stage_from_settings",
    "run_pipeline_stage_a_from_settings",
    "run_pipeline_stage_b_from_settings",
    "run_pipeline_stage_c_from_settings",
    "run_pipeline_stage_d_from_settings",
    "start_pipeline_run_from_settings",
]
