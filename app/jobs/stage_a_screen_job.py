"""Durable Stage A (lightweight AI screening) orchestration.

The stage deliberately has no knowledge of Stage B.  It persists one task and
one immutable attempt per item, and a successful screen task is the only input
that the analysis stage is allowed to consume later.
"""

from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

import httpx
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.skills.intel_triage import (
    RawIntelEnvelope,
    ScreenResult,
    apply_screen_guard,
    preflight_intel_triage_schemas,
    strict_parse_screen,
)
from app.config.limits import (
    DEFAULT_AI_REVIEW_LIMIT,
    DEFAULT_AI_REVIEW_CONCURRENCY,
    DEFAULT_AI_SCREEN_REJECT_THRESHOLD,
)
from app.domain.models import SourceSpec
from app.domain.recency import (
    STAGE_A_FRESHNESS_CUTOFF_MODE,
    STAGE_A_FRESHNESS_POLICY_VERSION,
    STAGE_A_FRESHNESS_TIMEZONE,
    StageAFreshnessDecision,
    stage_a_cutoff_at,
    stage_a_time_decision,
)
from app.jobs.provider_retry import ProviderResponseFailure, ProviderRetryExhausted, call_with_provider_retries
from app.storage.models import IntelItem, IntelRun, IntelRunStageTask
from app.storage.repository import IntelRepository

LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class StageAScreenResult:
    run_id: int | None = None
    processed: int = 0
    screened: int = 0
    screened_out: int = 0
    time_filtered: int = 0
    time_filter_counts: dict[str, int] = field(default_factory=dict)
    screen_failed: int = 0
    skipped: int = 0
    partial: bool = False
    partial_reason: str | None = None
    item_ids: list[int] = field(default_factory=list)
    eligible_item_ids: list[int] = field(default_factory=list)
    edition_date: str | None = None
    reference_time: datetime | None = None
    cutoff_at: datetime | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.screen_failed

    @property
    def run_counts(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "screened": self.screened,
            "screen_count": self.screened,
            "screened_out": self.screened_out,
            "time_filtered": self.time_filtered,
            "time_filter_counts": dict(self.time_filter_counts),
            "screen_failed": self.screen_failed,
            "partial": self.partial,
            "partial_reason": self.partial_reason,
        }


@dataclass(frozen=True)
class _ItemContext:
    item_id: int
    input_fingerprint: str
    envelope: RawIntelEnvelope | None
    source_spec: SourceSpec | None
    structural_error: str | None = None


@dataclass(frozen=True)
class _TimeFilteredItem:
    item_id: int
    input_fingerprint: str
    decision: StageAFreshnessDecision


class _TaskLeaseLost(RuntimeError):
    """Raised when a provider result can no longer be committed by its owner."""


def run_stage_a_screen_job(
    *,
    session_factory: sessionmaker[Session],
    source_specs: Mapping[str, SourceSpec] | None = None,
    ai_client: Any | None = None,
    run_id: int,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    ai_limit: int | None = None,
    force: bool = False,
    retry_failed: bool = False,
    retry: bool | None = None,
    include_blocked: bool = False,
    item_ids: Iterable[int] | None = None,
    task_ids: Iterable[int] | None = None,
    screen_reject_threshold: int = DEFAULT_AI_SCREEN_REJECT_THRESHOLD,
    concurrency: int = DEFAULT_AI_REVIEW_CONCURRENCY,
    dry_run: bool = False,
    now: Any | None = None,
    owner: str | None = None,
    progress: ProgressCallback | None = None,
    **_: Any,
) -> StageAScreenResult:
    """Run Stage A with durable per-item state.

    ``retry_failed`` only affects this stage.  In particular, this function
    never creates or invokes a Stage B task/provider call.
    """

    max_workers = _bounded_concurrency(concurrency)
    if retry is not None:
        retry_failed = bool(retry)
    selected_limit = _normalise_limit(ai_limit if ai_limit is not None else limit)
    reject_threshold = _bounded_score(screen_reject_threshold, DEFAULT_AI_SCREEN_REJECT_THRESHOLD)
    result = StageAScreenResult(run_id=run_id)
    owner = owner or f"stage-a-{uuid4().hex}"
    specs = dict(source_specs or {})

    # Local strict-schema validation is intentionally before any provider
    # request.  A malformed nested schema must fail fast for the whole stage.
    preflight_intel_triage_schemas()

    (
        effective_run_id,
        all_contexts,
        time_filtered,
        eligible_exceeds_limit,
        edition_date,
        reference_time,
        cutoff_at,
    ) = _prepare_scope(
        session_factory,
        run_id=run_id,
        source_specs=specs,
        limit=selected_limit,
        force=force,
        item_ids=item_ids,
        dry_run=dry_run,
        now=now,
    )
    result.run_id = effective_run_id
    result.edition_date = edition_date
    result.reference_time = reference_time
    result.cutoff_at = cutoff_at
    result.time_filtered = len(time_filtered)
    for entry in time_filtered:
        result.time_filter_counts[entry.decision.reason] = result.time_filter_counts.get(entry.decision.reason, 0) + 1
    if dry_run:
        contexts = all_contexts if selected_limit is None else all_contexts[:selected_limit]
        result.partial = eligible_exceeds_limit
        result.partial_reason = f"ai_limit:{selected_limit}" if result.partial else None
        result.item_ids = [context.item_id for context in contexts]
        result.processed = len(contexts)
        return result

    config_fingerprint = _config_fingerprint(
        stage="screen",
        model=getattr(ai_client, "model", None),
        reject_threshold=reject_threshold,
        freshness_policy=STAGE_A_FRESHNESS_POLICY_VERSION,
        freshness_cutoff_mode=STAGE_A_FRESHNESS_CUTOFF_MODE,
        freshness_timezone=STAGE_A_FRESHNESS_TIMEZONE,
    )
    task_ids_by_item: dict[int, int] = {}
    requested_task_ids = {int(value) for value in task_ids} if task_ids is not None else None
    with session_factory() as session:
        repo = IntelRepository(session)
        existing_stage = repo.get_stage(effective_run_id, "screen")
        # Only an explicit item/task retry may narrow the current build.
        stage_force = (
            force
            and item_ids is None
            and requested_task_ids is None
        )
        stage_metadata = {
            "reject_threshold": reject_threshold,
            "freshness_window_hours": None,
            "freshness_policy": STAGE_A_FRESHNESS_POLICY_VERSION,
            "freshness_cutoff_mode": STAGE_A_FRESHNESS_CUTOFF_MODE,
            "freshness_timezone": STAGE_A_FRESHNESS_TIMEZONE,
            "freshness_edition_date": edition_date,
            "reference_time": reference_time.isoformat() if reference_time else None,
            "cutoff_at": result.cutoff_at.isoformat() if result.cutoff_at else None,
            "freshness_counts": dict(result.time_filter_counts),
            "eligible_total": len(all_contexts),
        }
        stage = repo.ensure_stage(
            effective_run_id,
            "screen",
            config_fingerprint=config_fingerprint,
            # The storage API resets existing tasks for ``force``.  Do not
            # ask it to reset a just-created row before its auto-increment ID
            # has been flushed.
            force=stage_force if existing_stage is not None else False,
            metadata=stage_metadata,
        )
        if retry_failed:
            repo.retry_failed(stage, include_blocked=include_blocked)
        runnable_contexts: list[_ItemContext] = []
        for context in all_contexts:
            existing = None
            if requested_task_ids is not None:
                existing = repo.get_task(stage, subject_type="item", subject_id=context.item_id)
                if existing is None or existing.id not in requested_task_ids:
                    continue
            task = repo.ensure_stage_task(
                stage,
                subject_type="item",
                subject_id=context.item_id,
                item_id=context.item_id,
                input_fingerprint=context.input_fingerprint,
                config_fingerprint=config_fingerprint,
                force=bool(
                    (force and not stage_force)
                    or (existing is not None and existing.status in {"skipped", "cancelled"})
                ),
            )
            task_ids_by_item[context.item_id] = int(task.id)
            if _task_needs_provider_call(
                repo,
                task,
                input_fingerprint=context.input_fingerprint,
                config_fingerprint=config_fingerprint,
            ):
                runnable_contexts.append(context)
        for filtered in time_filtered:
            task = repo.ensure_stage_task(
                stage,
                subject_type="item",
                subject_id=filtered.item_id,
                item_id=filtered.item_id,
                input_fingerprint=filtered.input_fingerprint,
                config_fingerprint=config_fingerprint,
                force=force,
            )
            repo.complete_stage_task(
                task,
                status="skipped",
                result_ref={"projection": "StageAFreshnessDecision", "item_id": filtered.item_id},
                result={"decision": "reject", **filtered.decision.metadata()},
                metadata=filtered.decision.metadata(),
            )
            repo.update_run_item_status(
                effective_run_id,
                filtered.item_id,
                status=f"time_{filtered.decision.reason}",
            )
        # Limit only the *remaining* tasks.  Previously completed prefix
        # items do not consume the next batch, so ``--limit 1`` can resume its
        # tail instead of repeatedly selecting the same first item.
        result.partial = selected_limit is not None and len(runnable_contexts) > selected_limit
        result.partial_reason = f"ai_limit:{selected_limit}" if result.partial else None
        contexts = runnable_contexts if selected_limit is None else runnable_contexts[:selected_limit]
        result.item_ids = [context.item_id for context in contexts]
        result.processed = len(contexts)
        stage_metadata.update(
            {
                "runnable_total": len(runnable_contexts),
                "processed": len(contexts),
                "truncated_by_limit": result.partial,
            }
        )
        # Metadata is part of the current active projection as well.  A
        # second idempotent ensure only merges metadata; it does not reset the
        # tasks just materialized above.
        repo.ensure_stage(
            effective_run_id,
            "screen",
            config_fingerprint=config_fingerprint,
            metadata=stage_metadata,
        )
        if not all_contexts and not time_filtered:
            repo.finish_stage(stage, status="succeeded", metadata=stage_metadata)
        session.commit()

    progress_current = 0
    progress_total = len(contexts)
    _emit_screen_progress(progress, total=progress_total, current=progress_current)

    def advance_progress() -> None:
        nonlocal progress_current
        progress_current += 1
        _emit_screen_progress(progress, total=progress_total, current=progress_current)

    def persist_outcome(
        context: _ItemContext,
        task_id: int,
        screen: ScreenResult,
        failure: BaseException | None,
    ) -> None:
        """Persist one completed outcome on the coordinator thread."""

        if failure is not None or screen.status == "screen_failed":
            retryable, category, code, message = _classify_provider_failure(
                failure,
                code=screen.error_code,
                message=screen.error_message,
            )
            try:
                persisted = _persist_screen_failure(
                    session_factory,
                    task_id,
                    item_id=context.item_id,
                    run_id=effective_run_id,
                    screen=screen,
                    owner=owner,
                    model=getattr(ai_client, "model", None),
                    retryable=retryable,
                    error_category=category,
                    error_code=code,
                    error_message=message,
                )
            except _TaskLeaseLost as exc:
                result.skipped += 1
                result.errors.append(f"intel_item_id={context.item_id}: {exc}")
                return
            if persisted:
                result.screen_failed += 1
                result.errors.append(f"intel_item_id={context.item_id}: {message}")
            else:
                result.errors.append(f"intel_item_id={context.item_id}: screen persistence failed")
            return

        try:
            with session_factory() as session:
                repo = IntelRepository(session)
                task = session.get(IntelRunStageTask, task_id)
                if task is None:
                    raise RuntimeError(f"stage A task {task_id} disappeared")
                heartbeated = repo.heartbeat_stage_task(task, owner=owner)
                if heartbeated is None:
                    raise _TaskLeaseLost(f"stage A task {task_id} lease/owner lost before persistence")
                completed = repo.complete_stage_task(
                    task,
                    owner=owner,
                    result_ref={"projection": "AIItemScreen", "item_id": context.item_id},
                    result=screen.model_dump(mode="json"),
                    raw_response=screen.raw_response,
                    metadata={"decision": screen.decision, "confidence": screen.confidence},
                )
                if completed is None:
                    raise _TaskLeaseLost(f"stage A task {task_id} lease/owner lost before completion")
                # Complete the task only after the lease check above.  Keep
                # projection and task state in this same transaction so a
                # stale worker cannot publish a result for a task it no
                # longer owns.
                repo.upsert_ai_screen(
                    context.item_id,
                    screen,
                    run_id=effective_run_id,
                    model=getattr(ai_client, "model", None),
                    status="success",
                )
                screened_out = screen.decision == "reject" and screen.confidence >= reject_threshold
                if screened_out:
                    repo.set_item_status(context.item_id, "screened_out", run_id=effective_run_id)
                else:
                    # Keep the current build item in ``new`` until Stage B.
                    repo.set_item_status(context.item_id, "new", run_id=effective_run_id)
                session.commit()
            if screened_out:
                result.screened_out += 1
            else:
                result.eligible_item_ids.append(context.item_id)
            result.screened += 1
        except _TaskLeaseLost as exc:
            # Another worker may recover/retry this task.  Do not count a
            # stale result as a success and, importantly, do not attempt to
            # mark a task owned by someone else as failed.
            result.skipped += 1
            result.errors.append(f"intel_item_id={context.item_id}: {exc}")
        except Exception as exc:
            # Persistence failures are item-local and must not abort the batch.
            result.screen_failed += 1
            result.errors.append(f"intel_item_id={context.item_id}: screen persistence failed: {exc}")
            _mark_persistence_failure(session_factory, task_id, owner=owner, message=str(exc))
            LOGGER.exception("Stage A persistence failed for intel item %s", context.item_id)

    provider_contexts: list[tuple[_ItemContext, int]] = []
    for context in contexts:
        task_id = task_ids_by_item.get(context.item_id)
        if task_id is None:
            continue
        if context.structural_error is not None:
            # Structural failures are local and cheap; claim immediately
            # before persisting rather than leasing the entire queue.
            if _claim_task(
                session_factory,
                task_id,
                owner=owner,
                input_fingerprint=context.input_fingerprint,
                config_fingerprint=config_fingerprint,
            ):
                persist_outcome(
                    context,
                    task_id,
                    _structural_screen(context.item_id, context.structural_error),
                    None,
                )
                advance_progress()
            else:
                result.skipped += 1
                if _task_is_eligible(session_factory, task_id):
                    result.eligible_item_ids.append(context.item_id)
                advance_progress()
            continue
        provider_contexts.append((context, task_id))

    # Keep at most ``max_workers`` provider calls in flight.  A task is
    # claimed immediately before submission, so queued work cannot consume
    # its lease while waiting behind earlier provider calls.
    if provider_contexts:
        pending = iter(provider_contexts)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="intel-stage-a") as executor:
            futures: dict[Any, tuple[_ItemContext, int]] = {}

            def submit_next() -> bool:
                for context, task_id in pending:
                    if not _claim_task(
                        session_factory,
                        task_id,
                        owner=owner,
                        input_fingerprint=context.input_fingerprint,
                        config_fingerprint=config_fingerprint,
                    ):
                        result.skipped += 1
                        if _task_is_eligible(session_factory, task_id):
                            result.eligible_item_ids.append(context.item_id)
                        advance_progress()
                        continue
                    futures[executor.submit(
                        _screen_provider_outcome,
                        ai_client,
                        context,
                        reject_threshold,
                    )] = (context, task_id)
                    return True
                return False

            while len(futures) < max_workers and submit_next():
                pass
            while futures:
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    context, task_id = futures.pop(future)
                    screen, failure = future.result()
                    persist_outcome(context, task_id, screen, failure)
                    advance_progress()
                    submit_next()

    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(effective_run_id, "screen")
        if stage is not None:
            repo.recover_expired_stage_tasks(stage)
            repo.refresh_stage_status(stage)
            session.commit()
    return result


def _prepare_scope(
    session_factory: sessionmaker[Session],
    *,
    run_id: int,
    source_specs: Mapping[str, SourceSpec],
    limit: int | None,
    force: bool,
    item_ids: Iterable[int] | None,
    dry_run: bool,
    now: Any | None,
) -> tuple[int, list[_ItemContext], list[_TimeFilteredItem], bool, str, datetime, datetime]:
    """Freeze/resolve the item scope and build detached provider envelopes."""

    requested_ids = {int(value) for value in item_ids} if item_ids is not None else None
    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        if run.edition_id is None:
            raise ValueError("Stage A requires the current daily edition build")
        edition_date = run.edition_date
        if not edition_date:
            raise ValueError("Stage A requires a valid daily edition date")
        candidates = repo.list_run_items(run_id, role=None)
        if requested_ids is not None:
            candidates = [item for item in candidates if int(item.id) in requested_ids]
        reference_time = _as_utc(run.reference_time) or _as_utc(now) or datetime.now(timezone.utc)
        eligible, filtered = _filter_recent_items(
            candidates,
            source_specs=source_specs,
            reference_time=reference_time,
            edition_date=edition_date,
        )
        cutoff_at = stage_a_cutoff_at(edition_date)
        truncated_by_limit = limit is not None and len(eligible) > limit
        if dry_run:
            return (
                run_id,
                [_context_from_item(item, source_specs) for item in eligible],
                filtered,
                truncated_by_limit,
                edition_date,
                reference_time,
                cutoff_at,
            )
        contexts = [_context_from_item(item, source_specs) for item in eligible]
        return run_id, contexts, filtered, truncated_by_limit, edition_date, reference_time, cutoff_at


def _filter_recent_items(
    candidates: Iterable[IntelItem],
    *,
    source_specs: Mapping[str, SourceSpec],
    reference_time: datetime,
    edition_date: str,
) -> tuple[list[IntelItem], list[_TimeFilteredItem]]:
    eligible: list[IntelItem] = []
    filtered: list[_TimeFilteredItem] = []
    for item in candidates:
        source_spec = source_specs.get(item.source_id) or _spec_from_row(item.source)
        decision = stage_a_time_decision(
            item,
            source=source_spec,
            reference_time=reference_time,
            edition_date=edition_date,
        )
        if decision.eligible:
            eligible.append(item)
        else:
            filtered.append(
                _TimeFilteredItem(
                    item_id=int(item.id),
                    input_fingerprint=_item_fingerprint(item),
                    decision=decision,
                )
            )
    return eligible, filtered


def _context_from_item(item: IntelItem, source_specs: Mapping[str, SourceSpec]) -> _ItemContext:
    input_fingerprint = _item_fingerprint(item)
    source_spec = source_specs.get(item.source_id)
    if source_spec is None:
        source_spec = _spec_from_row(item.source)
    if not _structurally_valid(item):
        return _ItemContext(
            item_id=int(item.id),
            input_fingerprint=input_fingerprint,
            envelope=None,
            source_spec=source_spec,
            structural_error="item failed the structural prefilter",
        )
    try:
        envelope = _item_to_envelope(item, source_spec)
    except Exception as exc:
        return _ItemContext(
            item_id=int(item.id),
            input_fingerprint=input_fingerprint,
            envelope=None,
            source_spec=source_spec,
            structural_error=str(exc)[:4000] or exc.__class__.__name__,
        )
    return _ItemContext(int(item.id), input_fingerprint, envelope, source_spec)


def _task_needs_provider_call(
    repo: IntelRepository,
    task: IntelRunStageTask,
    *,
    input_fingerprint: str,
    config_fingerprint: str,
) -> bool:
    """Return whether this invocation can advance a materialized task.

    Successful reusable work is intentionally skipped before applying a
    numeric provider cap.  That lets a later capped invocation advance the
    next pending item rather than repeatedly attempting the already-complete
    prefix.  Blocked/running work remains observable but does not consume the
    cap until an explicit retry or lease recovery makes it runnable again.
    """

    if repo.task_is_reusable(
        task,
        input_fingerprint=input_fingerprint,
        config_fingerprint=config_fingerprint,
    ):
        return False
    return task.status in {"pending", "retry_waiting", "failed"}


def _call_screen(client: Any, envelope: RawIntelEnvelope | None, *, reject_threshold: int) -> ScreenResult:
    if envelope is None:
        raise ValueError("raw intel envelope is missing")
    method = getattr(client, "screen", None)
    if not callable(method):
        raise TypeError("AI client does not expose screen")
    value = method(envelope)
    if isinstance(value, ScreenResult):
        parsed = value.with_item(envelope)
    else:
        parsed = strict_parse_screen(value, envelope=envelope, reject_threshold=reject_threshold)
    return apply_screen_guard(parsed.with_item(envelope), envelope, reject_threshold=reject_threshold)


def _screen_provider_outcome(
    client: Any,
    context: _ItemContext,
    reject_threshold: int,
) -> tuple[ScreenResult, BaseException | None]:
    """Execute one provider task with bounded transient-error retries."""

    def operation() -> ScreenResult:
        value = _call_screen(client, context.envelope, reject_threshold=reject_threshold)
        if value.status == "screen_failed":
            retryable, _, _, _ = _classify_provider_failure(
                None,
                code=value.error_code,
                message=value.error_message,
            )
            if retryable:
                raise ProviderResponseFailure(value)
        return value

    value, failure, attempts = call_with_provider_retries(
        operation,
        is_retryable=lambda exc: _classify_provider_failure(
            exc,
            code=getattr(exc, "error_code", None),
            message=str(exc),
        )[0],
        stage="stage-a",
    )
    if failure is not None:
        return _screen_failure(context.item_id, failure, attempts=attempts), failure
    return value, None


def _screen_failure(item_id: int, exc: BaseException, *, attempts: int = 1) -> ScreenResult:
    message = str(exc).strip() or exc.__class__.__name__
    raw_response = getattr(exc, "raw_response", None)
    if not isinstance(raw_response, dict):
        raw_response = {}
    raw_response = {**raw_response, "provider_attempts": int(attempts)}
    return ScreenResult(
        item_id=item_id,
        decision="uncertain",
        reason_code="provider_failure",
        reason="Stage A provider call failed",
        confidence=0,
        risk_flags=["ai:screen_failed"],
        status="screen_failed",
        error_code=getattr(exc, "error_code", None) or exc.__class__.__name__,
        error_message=message[:4000],
        raw_response=raw_response,
    )


def _structural_screen(item_id: int, error: str) -> ScreenResult:
    return ScreenResult(
        item_id=item_id,
        decision="reject",
        reason_code="structural_invalid",
        reason=error[:4000],
        confidence=100,
        risk_flags=["prefilter:structural_invalid"],
        raw_response={"prefilter": "structural_invalid", "error": error},
    )


def _persist_screen_failure(
    session_factory: sessionmaker[Session],
    task_id: int,
    *,
    item_id: int,
    run_id: int,
    screen: ScreenResult,
    owner: str,
    model: str | None,
    retryable: bool,
    error_category: str,
    error_code: str,
    error_message: str,
) -> bool:
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            task = session.get(IntelRunStageTask, task_id)
            if task is None:
                raise RuntimeError(f"stage A task {task_id} disappeared")
            heartbeated = repo.heartbeat_stage_task(task, owner=owner)
            if heartbeated is None:
                raise _TaskLeaseLost(f"stage A task {task_id} lease/owner lost before failure persistence")
            failed = repo.fail_stage_task(
                task,
                owner=owner,
                error_category=error_category,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                raw_response=screen.raw_response,
            )
            if failed is None:
                raise _TaskLeaseLost(f"stage A task {task_id} lease/owner lost before failure persistence")
            # Persist the projection only after the owner/lease check above,
            # in the same transaction as the task failure transition.
            repo.upsert_ai_screen(
                item_id,
                screen,
                run_id=run_id,
                model=model,
                status="screen_failed",
                error_message=error_message,
            )
            repo.set_item_status(item_id, "screen_failed", run_id=run_id)
            session.commit()
        return True
    except _TaskLeaseLost:
        raise
    except Exception:
        LOGGER.exception("Stage A failure persistence failed for intel item %s", item_id)
        return False


def _mark_persistence_failure(session_factory: sessionmaker[Session], task_id: int, *, owner: str, message: str) -> None:
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            task = session.get(IntelRunStageTask, task_id)
            if task is not None:
                repo.fail_stage_task(
                    task,
                    owner=owner,
                    error_category="persistence",
                    error_code="persistence_failed",
                    error_message=message[:4000],
                    retryable=False,
                )
                session.commit()
    except Exception:
        LOGGER.exception("Unable to persist Stage A item-local failure for task %s", task_id)


def _claim_task(
    session_factory: sessionmaker[Session],
    task_id: int,
    *,
    owner: str,
    input_fingerprint: str,
    config_fingerprint: str,
) -> bool:
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            task = repo.claim_stage_task(
                task_id=task_id,
                stage_id=session.get(IntelRunStageTask, task_id).stage_id if session.get(IntelRunStageTask, task_id) else None,
                owner=owner,
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
            )
            if task is None:
                return False
            session.commit()
            return True
    except Exception:
        LOGGER.exception("Unable to claim Stage A task %s", task_id)
        return False


def _task_is_eligible(session_factory: sessionmaker[Session], task_id: int) -> bool:
    try:
        with session_factory() as session:
            task = session.get(IntelRunStageTask, task_id)
            if task is None or task.status != "succeeded":
                return False
            data = task.result
            return str(data.get("decision", "")).casefold() in {"pass", "uncertain"}
    except Exception:
        return False


def _classify_provider_failure(
    exc: BaseException | None,
    *,
    code: str | None,
    message: str | None,
) -> tuple[bool, str, str, str]:
    """Apply the frozen 429/timeout/5xx retry policy conservatively."""

    if isinstance(exc, ProviderRetryExhausted) or getattr(exc, "retry_exhausted", False):
        return (
            False,
            "provider_retry_exhausted",
            str(getattr(exc, "error_code", None) or code or "retry_exhausted")[:128],
            str(message or exc or "provider retries exhausted")[:4000],
        )

    response = getattr(exc, "response", None) if exc is not None else None
    status = getattr(response, "status_code", None) if response is not None else None
    if status is None and exc is not None:
        status = getattr(exc, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    text = " ".join(str(value or "") for value in (code, message, exc)).casefold()
    transient_text = any(
        token in text
        for token in (
            "429",
            "rate limit",
            "ratelimit",
            "5xx",
            "500",
            "502",
            "503",
            "504",
            "service unavailable",
            "bad gateway",
        )
    )
    if status_int == 429 or (status_int is not None and status_int >= 500) or transient_text:
        return True, "provider_retryable", str(status_int or code or "provider_retryable")[:128], str(message or exc or "provider unavailable")[:4000]
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or any(token in text for token in ("timeout", "timed out", "temporarily unavailable", "rate limit", "ratelimit")):
        return True, "provider_retryable", str(code or (status_int or "timeout"))[:128], str(message or exc or "provider timeout")[:4000]
    if status_int is not None and 400 <= status_int < 500:
        if any(token in text for token in ("validation", "schema", "invalid json", "missing required")):
            category = "schema"
        else:
            category = "provider_auth" if status_int in {401, 403} or "auth" in text else "provider_blocked"
        return False, category, str(status_int)[:128], str(message or exc or "provider request blocked")[:4000]
    if isinstance(exc, (ValidationError, ValueError, TypeError, json.JSONDecodeError)) or any(token in text for token in ("validation", "schema", "invalid json", "missing required")):
        return False, "schema", str(code or (exc.__class__.__name__ if exc else "schema_error"))[:128], str(message or exc or "provider schema validation failed")[:4000]
    # Unknown provider failures are blocked unless they carry an explicit
    # transient hint.  This prevents hidden retry loops for permanent errors.
    return False, "provider_blocked", str(code or (exc.__class__.__name__ if exc else "provider_error"))[:128], str(message or exc or "provider request failed")[:4000]


def _item_to_envelope(item: IntelItem, spec: SourceSpec) -> RawIntelEnvelope:
    source = item.source
    return RawIntelEnvelope(
        item_id=item.id,
        source_id=item.source_id,
        source_name=source.name if source is not None else spec.name,
        source_group=spec.source_group or (source.source_group if source is not None else None),
        source_content_class=spec.content_class,
        external_id=item.external_id,
        content_hash=item.content_hash,
        title=item.title,
        url=item.canonical_url,
        published_at=item.published_at,
        captured_at=item.captured_at,
        summary=item.summary,
        body_text=item.content_text or item.summary,
        metrics=_json_dict(item.metrics_json),
        raw_payload=_json_dict(item.raw_payload_json),
    )


def _spec_from_row(row: Any) -> SourceSpec:
    if row is None:
        return SourceSpec.model_validate(
            {"id": "unknown", "name": "unknown", "transport": "feed", "url": "https://invalid.local/", "content_class": "community_social"}
        )
    data: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "url": row.url,
        "enabled": row.enabled,
        "priority": row.priority,
        "fetch_interval": row.fetch_interval,
        "default_limit": row.default_limit,
        "source_group": row.source_group,
        "content_class": row.content_class,
    }
    if row.transport in {"feed", "rsshub"}:
        data["feed"] = {"format": row.feed_format or "rss", "adapter": row.feed_adapter or "generic"}
    elif row.transport == "github":
        github: dict[str, Any] = {"mode": row.github_mode or "search"}
        for name in ("query", "sort", "order", "pushed_days", "period"):
            value = getattr(row, f"github_{name}", None)
            if value is not None:
                github[name] = value
        data["github"] = github
    return SourceSpec.model_validate(data)


def _structurally_valid(item: IntelItem) -> bool:
    return bool(str(item.source_id or "").strip() and str(item.title or "").strip() and str(item.content_hash or "").strip())


def _item_fingerprint(item: IntelItem) -> str:
    value = item.content_hash or "|".join(str(part or "") for part in (item.source_id, item.title, item.canonical_url, item.summary, item.content_text))
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _config_fingerprint(
    *,
    stage: str,
    model: Any,
    reject_threshold: int,
    freshness_policy: str | None = None,
    freshness_cutoff_mode: str | None = None,
    freshness_timezone: str | None = None,
) -> str:
    payload = {"stage": stage, "model": str(model or ""), "reject_threshold": int(reject_threshold)}
    if freshness_policy:
        payload["freshness_policy"] = freshness_policy
    if freshness_cutoff_mode:
        payload["freshness_cutoff_mode"] = freshness_cutoff_mode
    if freshness_timezone:
        payload["freshness_timezone"] = freshness_timezone
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _emit_screen_progress(progress: ProgressCallback | None, *, total: int, current: int) -> None:
    if progress is None:
        return
    progress(
        {
            "type": "stage_update",
            "stage": "screen",
            "data": {
                "total": int(total),
                "current": int(current),
            },
        }
    )


def _bounded_score(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _bounded_concurrency(value: Any) -> int:
    try:
        return max(1, min(4, int(value)))
    except (TypeError, ValueError):
        return 4


def _normalise_limit(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


__all__ = [
    "StageAScreenResult",
    "run_stage_a_screen_job",
]
