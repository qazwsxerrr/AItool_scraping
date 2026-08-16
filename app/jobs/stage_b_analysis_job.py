"""Durable Stage B (full AI analysis) orchestration.

Only successful Stage-A pass/uncertain tasks for the same run are eligible.
The stage never invokes Stage A, which makes retrying an analysis provider
failure safe and inexpensive.
"""

from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.skills.intel_triage import (
    AnalysisResult,
    RawIntelEnvelope,
    analysis_guard_failure,
    apply_analysis_guards,
    preflight_intel_triage_schemas,
    strict_parse_analysis,
)
from app.config.limits import DEFAULT_AI_ANALYSIS_MIN_SCORE, DEFAULT_AI_REVIEW_CONCURRENCY, DEFAULT_AI_REVIEW_LIMIT
from app.domain.models import SourceSpec
from app.jobs.provider_retry import ProviderResponseFailure, call_with_provider_retries
from app.storage.models import IntelItem, IntelRunStageTask
from app.storage.repository import IntelRepository

from .stage_a_screen_job import (
    _classify_provider_failure,
    _config_fingerprint,
    _item_fingerprint,
    _item_to_envelope,
    _json_dict,
    _normalise_limit,
    _spec_from_row,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class StageBAnalysisResult:
    run_id: int | None = None
    processed: int = 0
    analyzed: int = 0
    analysis_filtered: int = 0
    analysis_failed: int = 0
    candidate: int = 0
    candidate_ids: list[int] = field(default_factory=list)
    skipped: int = 0
    partial: bool = False
    partial_reason: str | None = None
    item_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def selected(self) -> int:
        return self.candidate

    @property
    def failed(self) -> int:
        return self.analysis_failed

    @property
    def run_counts(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "analyzed": self.analyzed,
            "analyze_count": self.analyzed,
            "analysis_filtered": self.analysis_filtered,
            "analysis_failed": self.analysis_failed,
            "candidate": self.candidate,
            "partial": self.partial,
            "partial_reason": self.partial_reason,
        }


@dataclass(frozen=True)
class _AnalysisContext:
    item_id: int
    input_fingerprint: str
    envelope: RawIntelEnvelope
    content_class: str


class _TaskLeaseLost(RuntimeError):
    """Raised when a provider result can no longer be committed by its owner."""


def run_stage_b_analysis_job(
    *,
    session_factory: sessionmaker[Session],
    source_specs: Mapping[str, SourceSpec] | None = None,
    ai_client: Any | None = None,
    run_id: int,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    ai_limit: int | None = None,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    retry_failed: bool = False,
    retry: bool | None = None,
    include_blocked: bool = False,
    item_ids: Iterable[int] | None = None,
    task_ids: Iterable[int] | None = None,
    analysis_min_score: int = DEFAULT_AI_ANALYSIS_MIN_SCORE,
    concurrency: int = DEFAULT_AI_REVIEW_CONCURRENCY,
    owner: str | None = None,
    **_: Any,
) -> StageBAnalysisResult:
    """Run only persisted Stage-A eligible work for ``run_id``."""

    max_workers = _bounded_concurrency(concurrency)
    if retry is not None:
        retry_failed = bool(retry)
    selected_limit = _normalise_limit(ai_limit if ai_limit is not None else limit)
    min_score = _bounded_score(analysis_min_score, DEFAULT_AI_ANALYSIS_MIN_SCORE)
    result = StageBAnalysisResult(run_id=run_id)
    owner = owner or f"stage-b-{uuid4().hex}"
    specs = dict(source_specs or {})
    requested_ids = {int(value) for value in item_ids} if item_ids is not None else None
    requested_task_ids = {int(value) for value in task_ids} if task_ids is not None else None

    # Stage B has its own local contract preflight.  It must happen before any
    # analysis provider request and never causes Stage A to run.
    preflight_intel_triage_schemas()

    config_fingerprint = _config_fingerprint(
        stage="analyze",
        model=getattr(ai_client, "model", None),
        reject_threshold=min_score,
    )
    contexts: list[_AnalysisContext] = []
    task_ids_by_item: dict[int, int] = {}
    with session_factory() as session:
        repo = IntelRepository(session)
        existing_stage = repo.get_stage(run_id, "analyze")
        # Reset a whole stage only for a genuinely unfiltered rerun.  A
        # source/class/task filter is an operator-scoped retry and must not
        # leave unrelated Stage-B tasks pending.
        stage_force = (
            force
            and requested_ids is None
            and requested_task_ids is None
            and source_filter is None
            and content_class is None
        )
        stage = repo.ensure_stage(
            run_id,
            "analyze",
            config_fingerprint=config_fingerprint,
            force=stage_force if existing_stage is not None else False,
            metadata={"analysis_min_score": min_score},
        )
        if retry_failed:
            repo.retry_failed(stage, include_blocked=include_blocked, task_ids=requested_task_ids)

        screen_stage = repo.get_stage(run_id, "screen")
        if screen_stage is None:
            repo.finish_stage(stage, status="succeeded", metadata={"eligible": 0, "reason": "stage_a_missing"})
            session.commit()
            return result

        # We need all current Stage-A task states, not only successful ones:
        # a full Stage-B force resets prior task projections.  Old B work for
        # a Stage-A reject/time-filter must become ``skipped`` again, while a
        # still-pending Stage-A task must remain pending rather than being
        # silently treated as an ineligible terminal result.
        screen_tasks = repo.list_stage_tasks(screen_stage, subject_type="item", include_expired=True)
        screen_tasks_by_item = {
            int(task.item_id or task.subject_id): task
            for task in screen_tasks
            if task.item_id is not None or str(task.subject_id).isdigit()
        }
        scope_items = {int(item.id): item for item in repo.list_run_items(run_id, role="fetched")}
        eligible_contexts: list[_AnalysisContext] = []
        for screen_task in screen_tasks:
            if screen_task.status != "succeeded":
                continue
            item_id = int(screen_task.item_id or screen_task.subject_id)
            if requested_ids is not None and item_id not in requested_ids:
                continue
            existing_b = repo.get_task(stage, subject_type="item", subject_id=item_id)
            if requested_task_ids is not None:
                # ``task_ids`` names Stage-B tasks.  For a first invocation,
                # accepting a matching Stage-A ID is a small compatibility
                # affordance; otherwise keep the selection strictly scoped.
                if existing_b is not None and existing_b.id not in requested_task_ids:
                    continue
                if existing_b is None and screen_task.id not in requested_task_ids:
                    continue
            item = scope_items.get(item_id)
            if item is None:
                continue
            if screen_task.input_fingerprint and screen_task.input_fingerprint != _item_fingerprint(item):
                # A changed item invalidates the prior Stage-A decision; do
                # not let Stage B analyze stale content or silently rerun A.
                continue
            if source_filter and item.source_id != source_filter:
                continue
            if content_class and item.content_class != content_class:
                continue
            if not _screen_task_is_eligible(screen_task, run_id=run_id, session=session):
                continue
            spec = specs.get(item.source_id) or _spec_from_row(item.source)
            try:
                envelope = _item_to_envelope(item, spec)
            except Exception as exc:
                result.errors.append(f"intel_item_id={item_id}: analysis envelope failed: {exc}")
                continue
            eligible_contexts.append(
                _AnalysisContext(
                    item_id=item_id,
                    input_fingerprint=_item_fingerprint(item),
                    envelope=envelope,
                    content_class=item.content_class or spec.content_class or "community_social",
                )
            )

        if stage_force:
            active_eligible_item_ids = {context.item_id for context in eligible_contexts}
            for analysis_task in repo.list_stage_tasks(stage, subject_type="item", include_expired=True):
                item_id = analysis_task.item_id
                if item_id is None and str(analysis_task.subject_id).isdigit():
                    item_id = int(analysis_task.subject_id)
                if item_id is None:
                    continue
                screen_task = screen_tasks_by_item.get(int(item_id))
                if not _analysis_task_is_terminally_out_of_scope(
                    screen_task,
                    item_id=int(item_id),
                    active_eligible_item_ids=active_eligible_item_ids,
                    run_id=run_id,
                    session=session,
                ):
                    continue
                repo.complete_stage_task(
                    analysis_task,
                    status="skipped",
                    result_ref={"projection": "StageBEligibility", "item_id": int(item_id)},
                    result={
                        "reason": "stage_a_not_eligible",
                        "screen_task_status": screen_task.status if screen_task is not None else None,
                    },
                )
                result.skipped += 1

        # Materialize every current Stage-A eligible task before applying the
        # provider cap.  Items outside the slice remain pending, while already
        # successful items do not consume the next capped batch.
        runnable_contexts: list[_AnalysisContext] = []
        for context in eligible_contexts:
            existing_b = repo.get_task(stage, subject_type="item", subject_id=context.item_id)
            task = repo.ensure_stage_task(
                stage,
                subject_type="item",
                subject_id=context.item_id,
                item_id=context.item_id,
                input_fingerprint=context.input_fingerprint,
                config_fingerprint=config_fingerprint,
                # A formerly skipped task is an active projection only while
                # Stage A says it is ineligible.  If a later Stage-A rerun
                # admits it, reactivate it without requiring a schema change
                # or discarding its old attempt history.
                force=bool(
                    (force and not stage_force)
                    or (existing_b is not None and existing_b.status in {"skipped", "cancelled"})
                ),
            )
            task_ids_by_item[context.item_id] = int(task.id)
            if _analysis_task_needs_provider_call(
                repo,
                task,
                input_fingerprint=context.input_fingerprint,
                config_fingerprint=config_fingerprint,
            ):
                runnable_contexts.append(context)
        result.partial = selected_limit is not None and len(runnable_contexts) > selected_limit
        result.partial_reason = f"ai_limit:{selected_limit}" if result.partial else None
        contexts = runnable_contexts if selected_limit is None else runnable_contexts[:selected_limit]
        repo.ensure_stage(
            run_id,
            "analyze",
            config_fingerprint=config_fingerprint,
            metadata={
                "analysis_min_score": min_score,
                "eligible_total": len(eligible_contexts),
                "runnable_total": len(runnable_contexts),
                "processed": len(contexts),
                "truncated_by_limit": result.partial,
            },
        )
        session.commit()

    result.item_ids = [context.item_id for context in contexts]
    result.processed = len(contexts)

    def persist_outcome(
        context: _AnalysisContext,
        task_id: int,
        analysis: AnalysisResult,
        failure: BaseException | None,
    ) -> None:
        """Persist one completed outcome on the coordinator thread."""

        if failure is not None or analysis.status == "analysis_failed":
            retryable, category, code, message = _classify_provider_failure(
                failure,
                code=analysis.error_code,
                message=analysis.error_message,
            )
            try:
                persisted = _persist_analysis_failure(
                    session_factory,
                    task_id,
                    item_id=context.item_id,
                    run_id=run_id,
                    analysis=analysis,
                    content_class=context.content_class,
                    model=getattr(ai_client, "model", None),
                    owner=owner,
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
                result.analysis_failed += 1
                result.errors.append(f"intel_item_id={context.item_id}: {message}")
            else:
                result.errors.append(f"intel_item_id={context.item_id}: analysis persistence failed")
            return

        try:
            guard_reason = analysis_guard_failure(analysis)
            score = int(analysis.selection_score or 0)
            with session_factory() as session:
                repo = IntelRepository(session)
                task = session.get(IntelRunStageTask, task_id)
                if task is None:
                    raise RuntimeError(f"stage B task {task_id} disappeared")
                heartbeated = repo.heartbeat_stage_task(task, owner=owner)
                if heartbeated is None:
                    raise _TaskLeaseLost(f"stage B task {task_id} lease/owner lost before persistence")
                filtered = bool(guard_reason or score < min_score)
                persisted = analysis.model_copy(update={"reason": f"analysis_filtered:{guard_reason or 'score_below_threshold'}"}) if filtered else analysis
                completed = repo.complete_stage_task(
                    task,
                    owner=owner,
                    result_ref={"projection": "AIItemReview", "item_id": context.item_id},
                    result=persisted.model_dump(mode="json"),
                    raw_response=persisted.raw_response,
                    metadata={"selection_score": score, "filtered": filtered},
                )
                if completed is None:
                    raise _TaskLeaseLost(f"stage B task {task_id} lease/owner lost before completion")
                # Complete the task only after the lease check above.  Keep
                # projection and task state in this same transaction so a
                # stale worker cannot publish a result for a task it no
                # longer owns.
                if filtered:
                    repo.save_analysis(
                        context.item_id,
                        persisted,
                        run_id=run_id,
                        model=getattr(ai_client, "model", None),
                        content_class=context.content_class,
                        status="success",
                    )
                    item = session.get(IntelItem, context.item_id)
                    if item is not None:
                        item.selection_score = score
                        item.selection_reason = f"analysis_filtered:{guard_reason or 'score_below_threshold'}"
                    repo.set_item_status(context.item_id, "analysis_filtered", run_id=run_id)
                else:
                    repo.save_analysis(
                        context.item_id,
                        analysis,
                        run_id=run_id,
                        model=getattr(ai_client, "model", None),
                        content_class=context.content_class,
                        status="success",
                    )
                    item = session.get(IntelItem, context.item_id)
                    if item is not None:
                        item.selection_score = score
                        item.selection_reason = analysis.reason[:4000] if analysis.reason else "analysis_candidate"
                    repo.set_item_status(context.item_id, "candidate", run_id=run_id)
                session.commit()
            result.analyzed += 1
            if filtered:
                result.analysis_filtered += 1
            else:
                result.candidate += 1
                result.candidate_ids.append(context.item_id)
        except _TaskLeaseLost as exc:
            # Another worker may recover/retry this task.  Do not count a
            # stale result as a success and, importantly, do not attempt to
            # mark a task owned by someone else as failed.
            result.skipped += 1
            result.errors.append(f"intel_item_id={context.item_id}: {exc}")
        except Exception as exc:
            result.analysis_failed += 1
            result.errors.append(f"intel_item_id={context.item_id}: analysis persistence failed: {exc}")
            _mark_persistence_failure(session_factory, task_id, owner=owner, message=str(exc))
            LOGGER.exception("Stage B persistence failed for intel item %s", context.item_id)

    # Keep at most ``max_workers`` provider calls in flight.  A task is
    # claimed immediately before submission, so queued work cannot consume
    # its lease while waiting behind earlier provider calls.
    if contexts:
        pending = iter((context, task_ids_by_item[context.item_id]) for context in contexts)
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="intel-stage-b") as executor:
            futures: dict[Any, tuple[_AnalysisContext, int]] = {}

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
                        continue
                    futures[executor.submit(_analysis_provider_outcome, ai_client, context)] = (context, task_id)
                    return True
                return False

            while len(futures) < max_workers and submit_next():
                pass
            while futures:
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    context, task_id = futures.pop(future)
                    analysis, failure = future.result()
                    persist_outcome(context, task_id, analysis, failure)
                    submit_next()

    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "analyze")
        if stage is not None:
            repo.recover_expired_stage_tasks(stage)
            if contexts:
                repo.refresh_stage_status(stage)
            else:
                existing_tasks = repo.list_stage_tasks(stage, include_expired=True)
                if existing_tasks:
                    # Terminal skipped tasks represent the current Stage-A
                    # overlay; pending tasks are genuine remaining work.
                    repo.refresh_stage_status(stage)
                else:
                    repo.finish_stage(
                        stage,
                        status="succeeded",
                        metadata={
                            "analysis_min_score": min_score,
                            "eligible": 0,
                            "reason": "stage_a_no_eligible_items",
                        },
                    )
            session.commit()
    return result


run_stage_b = run_stage_b_analysis_job
run_analysis_stage = run_stage_b_analysis_job
run_stage_b_job = run_stage_b_analysis_job
run_stage_b_analysis = run_stage_b_analysis_job
run_stage_b_analyze = run_stage_b_analysis_job
StageBResult = StageBAnalysisResult


def _screen_task_is_eligible(task: IntelRunStageTask, *, run_id: int, session: Session) -> bool:
    data = task.result
    decision = str(data.get("decision", "")).casefold() if isinstance(data, Mapping) else ""
    if decision in {"pass", "uncertain"}:
        return True
    # Adopted tasks may only have a projection reference.  The projection is a
    # compatibility fallback and must still belong to this run.
    item = session.get(IntelItem, int(task.item_id or task.subject_id))
    screen = getattr(item, "ai_screen", None) if item is not None else None
    return bool(screen is not None and screen.run_id == int(run_id) and screen.status == "success" and screen.decision in {"pass", "uncertain"})


def _screen_task_is_terminally_ineligible(
    task: IntelRunStageTask | None,
    *,
    run_id: int,
    session: Session,
) -> bool:
    """Return whether the current Stage-A state deterministically excludes B.

    ``skipped`` is used for the run-local recent-window gate, while a
    successful reject is a completed AI screen.  Provider failures and
    pending work are deliberately *not* terminal here: their Stage-B task
    must remain pending/retryable instead of being erased by a forced rerun.
    """

    if task is None:
        return False
    if task.status in {"skipped", "cancelled"}:
        return True
    return task.status == "succeeded" and not _screen_task_is_eligible(
        task,
        run_id=run_id,
        session=session,
    )


def _analysis_task_is_terminally_out_of_scope(
    task: IntelRunStageTask | None,
    *,
    item_id: int,
    active_eligible_item_ids: set[int],
    run_id: int,
    session: Session,
) -> bool:
    """Return whether a forced Stage-B rerun must retire an old task.

    A current Stage-A pending/failure is deliberately left pending for later
    recovery.  Everything else that cannot enter this run's valid Stage-B
    scope—missing Stage-A lineage, a terminal reject/time gate, changed
    content, or an envelope that no longer builds—is terminally skipped so a
    stale task cannot keep the run partial forever.
    """

    if task is None:
        return True
    if _screen_task_is_terminally_ineligible(task, run_id=run_id, session=session):
        return True
    return task.status == "succeeded" and int(item_id) not in active_eligible_item_ids


def _analysis_task_needs_provider_call(
    repo: IntelRepository,
    task: IntelRunStageTask,
    *,
    input_fingerprint: str,
    config_fingerprint: str,
) -> bool:
    """Whether a current Stage-B task can advance in this invocation."""

    if repo.task_is_reusable(
        task,
        input_fingerprint=input_fingerprint,
        config_fingerprint=config_fingerprint,
    ):
        return False
    return task.status in {"pending", "retry_waiting", "failed"}


def _call_analysis(client: Any, envelope: RawIntelEnvelope) -> AnalysisResult:
    method = getattr(client, "analyze", None)
    if not callable(method):
        raise TypeError("AI client does not expose analyze")
    value = method(envelope)
    if isinstance(value, AnalysisResult):
        parsed = value.with_item(envelope)
    else:
        parsed = strict_parse_analysis(value, envelope=envelope)
    return apply_analysis_guards(parsed.with_item(envelope), envelope)


def _analysis_provider_outcome(
    client: Any,
    context: _AnalysisContext,
) -> tuple[AnalysisResult, BaseException | None]:
    """Execute one provider task with bounded transient-error retries."""

    def operation() -> AnalysisResult:
        value = _call_analysis(client, context.envelope)
        if value.status == "analysis_failed":
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
        stage="stage-b",
    )
    if failure is not None:
        return _analysis_failure(context.item_id, context.envelope, failure, attempts=attempts), failure
    return value, None


def _analysis_failure(
    item_id: int,
    envelope: RawIntelEnvelope,
    exc: BaseException,
    *,
    attempts: int = 1,
) -> AnalysisResult:
    message = str(exc).strip() or exc.__class__.__name__
    raw_response = getattr(exc, "raw_response", None)
    if not isinstance(raw_response, dict):
        raw_response = {}
    raw_response = {**raw_response, "provider_attempts": int(attempts)}
    return AnalysisResult(
        item_id=item_id,
        topic="opinion",
        topics=["opinion"],
        summary_cn="",
        keywords=[],
        entities=[],
        selection_score=0,
        score_components={},
        paper_support={"is_paper": False},
        risk_flags=["ai:analysis_failed"],
        reason="Stage B provider call failed",
        confidence=0,
        source_content_class=envelope.source_content_class,
        source_group=envelope.source_group,
        status="analysis_failed",
        error_code=getattr(exc, "error_code", None) or exc.__class__.__name__,
        error_message=message[:4000],
        raw_response=raw_response,
    )


def _persist_analysis_failure(
    session_factory: sessionmaker[Session],
    task_id: int,
    *,
    item_id: int,
    run_id: int,
    analysis: AnalysisResult,
    content_class: str,
    model: str | None,
    owner: str,
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
                raise RuntimeError(f"stage B task {task_id} disappeared")
            heartbeated = repo.heartbeat_stage_task(task, owner=owner)
            if heartbeated is None:
                raise _TaskLeaseLost(f"stage B task {task_id} lease/owner lost before failure persistence")
            failed = repo.fail_stage_task(
                task,
                owner=owner,
                error_category=error_category,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                raw_response=analysis.raw_response,
            )
            if failed is None:
                raise _TaskLeaseLost(f"stage B task {task_id} lease/owner lost before failure persistence")
            # Persist the projection only after the owner/lease check above,
            # in the same transaction as the task failure transition.
            repo.save_analysis(
                item_id,
                analysis,
                run_id=run_id,
                model=model,
                content_class=content_class,
                status="analysis_failed",
                error_message=error_message,
            )
            repo.set_item_status(item_id, "analysis_failed", run_id=run_id)
            session.commit()
        return True
    except _TaskLeaseLost:
        raise
    except Exception:
        LOGGER.exception("Stage B failure persistence failed for intel item %s", item_id)
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
        LOGGER.exception("Unable to persist Stage B item-local failure for task %s", task_id)


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
            row = session.get(IntelRunStageTask, task_id)
            if row is None:
                return False
            task = repo.claim_stage_task(
                task_id=task_id,
                stage_id=row.stage_id,
                owner=owner,
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
            )
            if task is None:
                return False
            session.commit()
            return True
    except Exception:
        LOGGER.exception("Unable to claim Stage B task %s", task_id)
        return False


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


__all__ = [
    "StageBAnalysisResult",
    "StageBResult",
    "run_stage_b_analysis_job",
    "run_stage_b_analysis",
    "run_stage_b_analyze",
    "run_stage_b",
    "run_stage_b_job",
    "run_analysis_stage",
]
