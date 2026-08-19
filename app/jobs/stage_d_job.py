"""Stage D: AI editorial selection over Stage-C canonical events.

Stage D is intentionally not another deterministic quota selector.  It keeps
only the paper evidence gate locally, then asks one dedicated editorial skill
to decide the complete daily combination.  Source/topic/repeat/card-count
preferences are context for the model, never local rejection quotas.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.skills.stage_d_editorial import (
    StageDAssessmentResponse,
    StageDCompositionResponse,
    StageDProviderCallResult,
    StageDEditorialClient,
    strict_parse_stage_d_assessment,
    strict_parse_stage_d_composition,
)
from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.config.settings import Settings
from app.domain.policies import is_first_party_x_source
from app.jobs.provider_retry import call_with_provider_retries
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    DailyEdition,
    DailyEditionReportEntry,
    IntelEvent,
    IntelEventItem,
    IntelEventStageDSnapshot,
    IntelItem,
    IntelRun,
    IntelRunStage,
    IntelRunStageTask,
    utcnow,
)
from app.storage.repository import IntelRepository


LOGGER = logging.getLogger(__name__)
STAGE_D_NAME = "stage_d"
STAGE_D_VERSION = "stage-d-v2"
STAGE_D_ASSESSMENT_PROMPT_VERSION = "stage_d_assessment_v1"
STAGE_D_COMPOSITION_PROMPT_VERSION = "stage_d_editorial_v2"
DEFAULT_STAGE_D_BATCH_SIZE = 24
DEFAULT_STAGE_D_CONCURRENCY = 2
DEFAULT_STAGE_D_SHORTLIST_MAX = 60
DEFAULT_STAGE_D_WATCHLIST_MAX = 10
DEFAULT_STAGE_D_ASSESSMENT_RETRIES = 5


class StageDExecutionError(RuntimeError):
    """Terminal Stage-D execution failure that must block downstream export."""

    def __init__(self, phase: str, message: str, *, cause: BaseException | None = None) -> None:
        self.phase = str(phase)
        self.cause = cause
        super().__init__(f"stage_d {self.phase} failed: {message}")


class StageDProviderCallError(RuntimeError):
    """Carry provider-attempt and sanitized response data through fallback."""

    def __init__(self, cause: BaseException, attempts: int) -> None:
        self.cause = cause
        self.provider_attempts = int(attempts)
        self.status_code = getattr(cause, "status_code", None)
        self.error_code = getattr(cause, "error_code", None)
        self.error_message = getattr(cause, "error_message", None) or str(cause)
        self.raw_response = getattr(cause, "raw_response", None)
        self.request_metadata = dict(getattr(cause, "request_metadata", None) or {})
        super().__init__(str(cause))


@dataclass(frozen=True)
class StageDProfile:
    """Durable Stage-D v2 policy shared by D1, D2 and D3."""

    total_max: int = DEFAULT_DAILY_REPORT_LIMIT
    paper_hard_gate: bool = True
    recent_history_days: int = 3
    version: str = STAGE_D_VERSION
    assessment_batch_size: int = DEFAULT_STAGE_D_BATCH_SIZE
    assessment_concurrency: int = DEFAULT_STAGE_D_CONCURRENCY
    shortlist_max: int = DEFAULT_STAGE_D_SHORTLIST_MAX
    watchlist_max: int = DEFAULT_STAGE_D_WATCHLIST_MAX
    assessment_retries: int = DEFAULT_STAGE_D_ASSESSMENT_RETRIES

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "StageDProfile":
        data = dict(value or {})
        paper = data.get("paper") if isinstance(data.get("paper"), Mapping) else {}
        return cls(
            total_max=_bounded_int(data.get("total_max"), DEFAULT_DAILY_REPORT_LIMIT, lower=0, upper=30),
            paper_hard_gate=_coerce_bool(data.get("paper_hard_gate", paper.get("hard_gate", True)), True),
            recent_history_days=_bounded_int(data.get("recent_history_days"), 3, lower=0, upper=30),
            version=str(data.get("version") or STAGE_D_VERSION),
            assessment_batch_size=_bounded_int(
                data.get("assessment_batch_size"),
                DEFAULT_STAGE_D_BATCH_SIZE,
                lower=1,
                upper=24,
            ),
            assessment_concurrency=_bounded_int(
                data.get("assessment_concurrency"),
                DEFAULT_STAGE_D_CONCURRENCY,
                lower=1,
                upper=2,
            ),
            shortlist_max=_bounded_int(
                data.get("shortlist_max"),
                DEFAULT_STAGE_D_SHORTLIST_MAX,
                lower=0,
                upper=60,
            ),
            watchlist_max=_bounded_int(
                data.get("watchlist_max"),
                DEFAULT_STAGE_D_WATCHLIST_MAX,
                lower=0,
                upper=30,
            ),
            assessment_retries=_bounded_int(
                data.get("assessment_retries"),
                DEFAULT_STAGE_D_ASSESSMENT_RETRIES,
                lower=0,
                upper=5,
            ),
        )


@dataclass
class StageDResult:
    run_id: int
    processed: int = 0
    eligible: int = 0
    selected: int = 0
    omitted: int = 0
    assessed: int = 0
    assessment_batches: int = 0
    shortlist_count: int = 0
    watchlist: int = 0
    paper_gated: int = 0
    snapshots: int = 0
    ai_selected: int = 0
    ai_failed: int = 0
    provider_attempts: int = 0
    assessment_provider_attempts: int = 0
    composition_provider_attempts: int = 0
    failed_phase: str | None = None
    errors: list[str] = field(default_factory=list)


def load_stage_d_profile(path: str | Path | None = None) -> StageDProfile:
    profile_path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / "config" / "daily_profile.yaml"
    if not profile_path.exists():
        return StageDProfile()
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        LOGGER.warning("Unable to read Stage D profile %s: %s", profile_path, exc)
        return StageDProfile()
    return StageDProfile.from_mapping(raw if isinstance(raw, Mapping) else None)


def run_stage_d_job(
    *,
    session_factory: sessionmaker[Session],
    profile: StageDProfile | Mapping[str, Any] | str | Path | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    force: bool = False,
    run_id: int,
    event_ids: Iterable[int] | None = None,
) -> StageDResult:
    """Run Stage D v2: assess batches, build a local shortlist, then compose."""

    policy = _coerce_profile(profile if profile is not None else profile_path)
    result = StageDResult(run_id=run_id)
    owner = "stage-d-editorial"
    stage: IntelRunStage | None = None
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id))
            if run is None or run.edition_id is None:
                raise ValueError("Stage D requires the current daily edition build")
            stage = repo.ensure_stage(
                int(run_id),
                STAGE_D_NAME,
                metadata=_stage_d_stage_metadata(policy, ai_client),
            )
            if event_ids is None:
                event_ids = _load_current_cluster_event_ids(session, int(run_id))
            events = _load_events(session, run_id=run_id, event_ids=event_ids)
            result.processed = len(events)
            candidates = [_candidate(event) for event in events]
            history = _recent_daily_history(session, candidates=candidates, run=run, days=policy.recent_history_days)
            for candidate in candidates:
                candidate["recent_daily_history"] = history.get(
                    int(candidate["event"].id), {"appeared_recently": False, "prior_editions": []}
                )

            eligible = [candidate for candidate in candidates if candidate["paper_gate_pass"] or not policy.paper_hard_gate]
            gated = [candidate for candidate in candidates if not (candidate["paper_gate_pass"] or not policy.paper_hard_gate)]
            result.eligible = len(eligible)
            result.paper_gated = len(gated)

            d1_config = _stage_d_phase_config(policy, ai_client, phase="assessment")
            d3_config = _stage_d_phase_config(policy, ai_client, phase="composition")
            batches = _stage_d_batch_specs(
                eligible,
                policy.assessment_batch_size,
                d1_config,
                seed=str(run_id),
            )
            result.assessment_batches = len(batches)
            assessments: dict[int, dict[str, Any]] = {}
            _mark_stale_stage_d_batches(repo, stage, {batch["subject_id"] for batch in batches})
            session.flush()

            pending_batches: list[dict[str, Any]] = []
            for batch in batches:
                batch_id = str(batch["subject_id"])
                task = None
                if stage is not None:
                    task = repo.ensure_stage_task(
                        stage,
                        subject_type="batch",
                        subject_id=batch_id,
                        input_fingerprint=str(batch["input_fingerprint"]),
                        config_fingerprint=d1_config,
                        metadata={"phase": "assessment", "batch_id": batch_id, "event_ids": batch["event_ids"]},
                    )
                    stored = _stored_assessments(task, batch["event_ids"], batch["input_fingerprint"], d1_config)
                    if stored is not None:
                        assessments.update(stored)
                        result.assessed += len(stored)
                        continue
                pending_batches.append({"batch": batch, "task": task})

            concurrency = max(1, min(policy.assessment_concurrency, len(pending_batches) or 1))
            for start in range(0, len(pending_batches), concurrency):
                wave = pending_batches[start : start + concurrency]
                provider_work: list[dict[str, Any]] = []
                for work in wave:
                    batch = work["batch"]
                    task = work["task"]
                    if stage is not None and task is not None:
                        claimed = repo.claim_stage_task(
                            stage,
                            task_id=task.id,
                            owner=owner,
                            input_fingerprint=str(batch["input_fingerprint"]),
                            config_fingerprint=d1_config,
                            acquire_stage=True,
                        )
                        if claimed is None:
                            if repo.task_is_reusable(
                                task,
                                input_fingerprint=batch["input_fingerprint"],
                                config_fingerprint=d1_config,
                            ):
                                stored = _stored_assessments(
                                    task,
                                    batch["event_ids"],
                                    batch["input_fingerprint"],
                                    d1_config,
                                )
                                if stored is not None:
                                    assessments.update(stored)
                                    result.assessed += len(stored)
                                    continue
                            raise StageDExecutionError(
                                "assessment",
                                f"batch task is already running: {batch['subject_id']}",
                            )
                        work["task"] = claimed
                    provider_work.append(work)
                if stage is not None and provider_work:
                    session.commit()

                outcomes: dict[str, tuple[Any, int, dict[str, Any]] | BaseException] = {}
                with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(provider_work)))) as executor:
                    futures = {
                        executor.submit(
                            _call_assessment_provider,
                            ai_client,
                            [_prompt_event(candidate) for candidate in work["batch"]["candidates"]],
                            edition={"date": run.edition_date if run is not None else None},
                            retries=policy.assessment_retries,
                        ): str(work["batch"]["subject_id"])
                        for work in provider_work
                    }
                    for future in as_completed(futures):
                        batch_id = futures[future]
                        try:
                            outcomes[batch_id] = future.result()
                        except BaseException as exc:  # persisted below on the coordinator thread
                            outcomes[batch_id] = exc

                first_failure: BaseException | None = None
                for work in provider_work:
                    batch = work["batch"]
                    task = work["task"]
                    batch_id = str(batch["subject_id"])
                    outcome = outcomes[batch_id]
                    if isinstance(outcome, BaseException):
                        first_failure = first_failure or outcome
                        result.ai_failed += 1
                        result.failed_phase = "assessment"
                        result.errors.append(str(outcome))
                        if task is not None:
                            failure_audit = _provider_audit(outcome, None)
                            task.result_json = json.dumps(
                                {
                                    "phase": "assessment",
                                    "batch_id": batch_id,
                                    "event_ids": batch["event_ids"],
                                    "provider_attempts": int(failure_audit.get("provider_attempts") or 0),
                                    "request_metadata": failure_audit.get("request_metadata") or {},
                                    "response_hash": _response_hash(failure_audit.get("raw_response")) if failure_audit.get("raw_response") is not None else None,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            repo.fail_stage_task(
                                task,
                                owner=owner,
                                error_category="provider",
                                error_code=getattr(outcome, "error_code", None) or "assessment_failed",
                                error_message=str(outcome),
                                retryable=False,
                                raw_response=getattr(outcome, "raw_response", None),
                            )
                        continue

                    parsed, attempts, audit = outcome
                    rows = _assessment_rows(parsed)
                    batch_assessments = {int(row["event_id"]): row for row in rows}
                    if set(batch_assessments) != set(batch["event_ids"]):
                        coverage_error = ValueError(f"batch coverage mismatch: {batch_id}")
                        first_failure = first_failure or coverage_error
                        result.ai_failed += 1
                        result.failed_phase = "assessment"
                        result.errors.append(str(coverage_error))
                        if task is not None:
                            repo.fail_stage_task(
                                task,
                                owner=owner,
                                error_category="schema",
                                error_code="assessment_coverage_mismatch",
                                error_message=str(coverage_error),
                                retryable=False,
                            )
                        continue
                    assessments.update(batch_assessments)
                    result.assessed += len(batch_assessments)
                    result.assessment_provider_attempts += attempts
                    result.provider_attempts += attempts
                    if task is not None:
                        repo.complete_stage_task(
                            task,
                            owner=owner,
                            result_ref={"phase": "assessment", "batch_id": batch_id},
                            result={
                                "phase": "assessment",
                                "batch_id": batch_id,
                                "event_ids": batch["event_ids"],
                                "input_fingerprint": batch["input_fingerprint"],
                                "config_fingerprint": d1_config,
                                "assessments": rows,
                                "provider_attempts": attempts,
                                "request_metadata": audit.get("request_metadata") or {},
                                "response_hash": _response_hash(audit.get("raw_response") or rows),
                            },
                            raw_response=audit.get("raw_response"),
                            metadata=audit,
                        )
                if stage is not None and provider_work:
                    session.commit()
                if first_failure is not None:
                    raise StageDExecutionError("assessment", str(first_failure), cause=first_failure) from first_failure

            shortlist = _build_stage_d_shortlist(eligible, assessments, shortlist_max=policy.shortlist_max)
            result.shortlist_count = len(shortlist)
            shortlist_by_id = {int(row["event_id"]): row for row in shortlist}
            shortlist_rank = {event_id: index for index, event_id in enumerate(shortlist_by_id, start=1)}
            d3_payload = [_prompt_shortlist_event(row["candidate"], row["assessment"], shortlist_rank[event_id]) for event_id, row in shortlist_by_id.items()]
            shortlist_fingerprint = _response_hash(
                {"events": d3_payload, "max_selected": policy.total_max, "max_watchlist": policy.watchlist_max}
            )
            decisions: dict[int, dict[str, Any]] = {}
            d3_task = None
            if stage is not None:
                d3_task = repo.ensure_stage_task(
                    stage,
                    subject_type="run",
                    subject_id=int(run_id),
                    target_run_id=int(run_id),
                    input_fingerprint=shortlist_fingerprint,
                    config_fingerprint=d3_config,
                    metadata={"phase": "composition", "shortlist_count": len(d3_payload)},
                )
                stored_decisions = _stored_composition(d3_task, shortlist_by_id, shortlist_fingerprint, d3_config)
                if stored_decisions is not None and not force:
                    decisions = stored_decisions
                else:
                    claimed = repo.claim_stage_task(
                        stage,
                        task_id=d3_task.id,
                        owner=owner,
                        force=bool(force),
                        input_fingerprint=shortlist_fingerprint,
                        config_fingerprint=d3_config,
                        acquire_stage=True,
                    )
                    if claimed is None:
                        raise StageDExecutionError("composition", "run task is already running")
                    d3_task = claimed
                    session.commit()
            if d3_task is None or not decisions:
                if d3_payload:
                    try:
                        parsed, attempts, audit = _call_composition_provider(
                            ai_client,
                            d3_payload,
                            edition={
                                "date": run.edition_date if run is not None else None,
                                "max_selected": policy.total_max,
                                "max_watchlist": policy.watchlist_max,
                                "max_selected_per_story_family": 2,
                            },
                            total_max=policy.total_max,
                            watchlist_max=policy.watchlist_max,
                            retries=_stage_d_composition_retries(ai_client),
                        )
                        decisions = _decision_rows(parsed)
                        result.composition_provider_attempts = attempts
                        result.provider_attempts += attempts
                        composition_audit = audit
                    except Exception as exc:
                        result.ai_failed += 1
                        result.failed_phase = "composition"
                        result.errors.append(str(exc))
                        if d3_task is not None:
                            repo.fail_stage_task(
                                d3_task,
                                owner=owner,
                                error_category="provider",
                                error_code=getattr(exc, "error_code", None) or "composition_failed",
                                error_message=str(exc),
                                retryable=False,
                                raw_response=getattr(exc, "raw_response", None),
                            )
                            session.commit()
                        raise StageDExecutionError("composition", str(exc), cause=exc) from exc
                else:
                    decisions = {}
                    result.composition_provider_attempts = 0
                    composition_audit = {"request_metadata": {}, "raw_response": None}
                if d3_task is not None:
                    decision_rows = list(decisions.values())
                    repo.complete_stage_task(
                        d3_task,
                        owner=owner,
                        result_ref={"phase": "composition"},
                        result={
                            "phase": "composition",
                            "event_ids": list(shortlist_by_id),
                            "input_fingerprint": shortlist_fingerprint,
                            "config_fingerprint": d3_config,
                            "decisions": decision_rows,
                            "provider_attempts": result.composition_provider_attempts,
                            "request_metadata": composition_audit.get("request_metadata") or {},
                            "response_hash": _response_hash(composition_audit.get("raw_response") or decision_rows),
                        },
                        raw_response=composition_audit.get("raw_response"),
                        metadata=composition_audit,
                    )
                    session.commit()

            # Replace build-local Stage-D rows only after all provider work
            # and exact D3 coverage have succeeded. DELETE+INSERT remains one
            # transaction, so rollback leaves the prior draft state untouched.
            _replace_stage_d_snapshot(
                repo,
                run_id=run_id,
                candidates=candidates,
                gated_event_ids={int(candidate["event"].id) for candidate in gated},
                assessments=assessments,
                shortlist_by_id=shortlist_by_id,
                shortlist_rank=shortlist_rank,
                decisions=decisions,
                policy=policy,
                result=result,
            )
            session.commit()
            stage_metadata = _stage_d_stage_metadata(
                policy,
                ai_client,
                assessment_batch_count=result.assessment_batches,
                assessed_count=result.assessed,
                shortlist_count=result.shortlist_count,
                selected_count=result.selected,
                watchlist_count=result.watchlist,
                omitted_count=result.omitted,
                provider_attempts=result.provider_attempts,
            )
            if stage is not None:
                repo.finish_stage(stage, status="succeeded", metadata=stage_metadata, owner=owner)
                session.commit()
            return result
    except StageDExecutionError:
        _persist_stage_d_failure(session_factory, run_id, result)
        raise
    except Exception as exc:
        result.failed_phase = result.failed_phase or "persistence"
        result.errors.append(str(exc))
        _persist_stage_d_failure(session_factory, run_id, result)
        LOGGER.exception("Stage D failed")
        raise StageDExecutionError(result.failed_phase, str(exc), cause=exc) from exc


def run_stage_d_from_settings(
    *,
    settings: Settings,
    profile: StageDProfile | Mapping[str, Any] | str | Path | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    force: bool = False,
    run_id: int,
    event_ids: Iterable[int] | None = None,
) -> StageDResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return run_stage_d_job(
        session_factory=create_session_factory(engine),
        profile=profile if profile is not None else profile_path,
        ai_client=ai_client if ai_client is not None else StageDEditorialClient.from_settings(settings),
        force=force,
        run_id=run_id,
        event_ids=event_ids,
    )


def _coerce_profile(value: StageDProfile | Mapping[str, Any] | str | Path | None) -> StageDProfile:
    if isinstance(value, StageDProfile):
        return value
    if isinstance(value, (str, Path)):
        return load_stage_d_profile(value)
    if isinstance(value, Mapping):
        return StageDProfile.from_mapping(value)
    return load_stage_d_profile()


def _stage_d_stage_metadata(
    policy: StageDProfile,
    ai_client: Any | None,
    **counts: Any,
) -> dict[str, Any]:
    metadata = {
        "profile_version": policy.version,
        "stage_d_version": STAGE_D_VERSION,
        "assessment_prompt_version": STAGE_D_ASSESSMENT_PROMPT_VERSION,
        "composition_prompt_version": STAGE_D_COMPOSITION_PROMPT_VERSION,
        "assessment_batch_size": policy.assessment_batch_size,
        "assessment_concurrency": policy.assessment_concurrency,
        "assessment_retries": policy.assessment_retries,
        "shortlist_max": policy.shortlist_max,
        "total_max": policy.total_max,
        "watchlist_max": policy.watchlist_max,
        "paper_hard_gate": policy.paper_hard_gate,
        "model": getattr(ai_client, "model", None),
    }
    metadata.update({key: int(value) for key, value in counts.items() if value is not None})
    return metadata


def _stage_d_phase_config(policy: StageDProfile, ai_client: Any | None, *, phase: str) -> str:
    prompt = STAGE_D_ASSESSMENT_PROMPT_VERSION if phase == "assessment" else STAGE_D_COMPOSITION_PROMPT_VERSION
    return f"{STAGE_D_VERSION}:{policy.version}:{phase}:{prompt}:{getattr(ai_client, 'model', None) or 'unconfigured'}"


def _stage_d_composition_retries(ai_client: Any | None) -> int:
    value = getattr(ai_client, "max_retries", None)
    if value is None:
        value = getattr(getattr(ai_client, "settings", None), "ai_stage_d_retries", 2)
    return _bounded_int(value, 2, lower=0, upper=5)


def _stage_d_batch_specs(
    candidates: Sequence[Mapping[str, Any]],
    batch_size: int,
    config_fingerprint: str,
    *,
    seed: str,
) -> list[dict[str, Any]]:
    ordered = sorted(
        candidates,
        key=lambda row: hashlib.sha256(
            f"{seed}:{int(row['event'].id)}".encode("utf-8")
        ).hexdigest(),
    )
    size = max(1, min(24, int(batch_size)))
    batches: list[dict[str, Any]] = []
    for start in range(0, len(ordered), size):
        rows = ordered[start : start + size]
        event_ids = [int(row["event"].id) for row in rows]
        membership = _response_hash({"event_ids": event_ids})
        payload = [_prompt_event(row) for row in rows]
        batches.append(
            {
                "subject_id": f"batch-{membership[:24]}",
                "event_ids": event_ids,
                "candidates": rows,
                "input_fingerprint": _response_hash({"config": config_fingerprint, "events": payload}),
            }
        )
    return batches


def _mark_stale_stage_d_batches(repo: IntelRepository, stage: IntelRunStage, active_ids: set[str]) -> None:
    for task in repo.list_stage_tasks(stage, subject_type="batch", include_expired=True):
        if task.subject_id in active_ids or task.status in {"skipped", "cancelled"}:
            continue
        task.status = "skipped"
        task.error_category = "plan"
        task.error_code = "stale_batch_plan"
        task.error_message = "batch 不再属于当前 Stage-C 输入计划。"
        task.lease_owner = None
        task.lease_expires_at = None
        task.heartbeat_at = None
        task.updated_at = utcnow()


def _assessment_rows(value: Any) -> list[dict[str, Any]]:
    assessments = getattr(value, "assessments", None)
    if assessments is None and isinstance(value, Mapping):
        assessments = value.get("assessments")
    rows: list[dict[str, Any]] = []
    for assessment in assessments or []:
        if hasattr(assessment, "model_dump"):
            rows.append(dict(assessment.model_dump(mode="json")))
        elif isinstance(assessment, Mapping):
            rows.append(dict(assessment))
    return rows


def _decision_rows(value: Any) -> dict[int, dict[str, Any]]:
    decisions = getattr(value, "decisions", None)
    if decisions is None and isinstance(value, Mapping):
        decisions = value.get("decisions")
    result: dict[int, dict[str, Any]] = {}
    for decision in decisions or []:
        row = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else dict(decision)
        result[int(row["event_id"])] = dict(row)
    return result


def _stored_assessments(
    task: IntelRunStageTask,
    event_ids: Sequence[int],
    input_fingerprint: str,
    config_fingerprint: str,
) -> dict[int, dict[str, Any]] | None:
    if not task or not _task_is_reusable(task, input_fingerprint, config_fingerprint):
        return None
    stored = task.result
    if not isinstance(stored, Mapping) or stored.get("phase") != "assessment":
        return None
    rows = _assessment_rows(stored)
    values = {int(row["event_id"]): row for row in rows if row.get("event_id") is not None}
    return values if set(values) == set(int(value) for value in event_ids) else None


def _stored_composition(
    task: IntelRunStageTask,
    shortlist_by_id: Mapping[int, Mapping[str, Any]],
    input_fingerprint: str,
    config_fingerprint: str,
) -> dict[int, dict[str, Any]] | None:
    if not task or not _task_is_reusable(task, input_fingerprint, config_fingerprint):
        return None
    stored = task.result
    if not isinstance(stored, Mapping) or stored.get("phase") != "composition":
        return None
    rows = _decision_rows(stored)
    return rows if set(rows) == set(int(value) for value in shortlist_by_id) else None


def _task_is_reusable(task: IntelRunStageTask, input_fingerprint: str, config_fingerprint: str) -> bool:
    return task.status == "succeeded" and task.input_fingerprint == str(input_fingerprint) and task.config_fingerprint == str(config_fingerprint)


def _assessment_score(row: Mapping[str, Any]) -> float:
    return round(
        0.25 * _number(row.get("material_change"))
        + 0.20 * _number(row.get("impact"))
        + 0.20 * _number(row.get("reader_value"))
        + 0.15 * _number(row.get("actionability"))
        + 0.10 * _number(row.get("source_support"))
        + 0.10 * _number(row.get("freshness")),
        4,
    )


def _build_stage_d_shortlist(
    candidates: Sequence[Mapping[str, Any]],
    assessments: Mapping[int, Mapping[str, Any]],
    *,
    shortlist_max: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        event_id = int(candidate["event"].id)
        assessment = assessments.get(event_id)
        if assessment is None:
            continue
        rows.append(
            {
                "event_id": event_id,
                "candidate": candidate,
                "assessment": dict(assessment),
                "score": _assessment_score(assessment),
                "must_consider": bool(assessment.get("must_consider")),
            }
        )
    limit = max(0, int(shortlist_max))
    ordered = sorted(rows, key=lambda row: (-row["score"], -_number(row["candidate"]["event"].display_score), row["event_id"]))
    if len(ordered) <= limit:
        return ordered
    selected_ids: set[int] = set()
    must = [row for row in ordered if row["must_consider"]]
    if len(must) >= limit:
        return must[:limit]
    selected: list[dict[str, Any]] = []
    for row in must:
        selected.append(row)
        selected_ids.add(row["event_id"])
    topics: dict[str, list[dict[str, Any]]] = {}
    for row in ordered:
        topic = str(row["candidate"].get("topic") or "opinion")
        topics.setdefault(topic, []).append(row)
    for topic_rows in topics.values():
        for row in topic_rows[:2]:
            if row["event_id"] in selected_ids or len(selected) >= limit:
                continue
            selected.append(row)
            selected_ids.add(row["event_id"])
    for row in ordered:
        if len(selected) >= limit:
            break
        if row["event_id"] not in selected_ids:
            selected.append(row)
            selected_ids.add(row["event_id"])
    return selected[:limit]


def _prompt_shortlist_event(candidate: Mapping[str, Any], assessment: Mapping[str, Any], rank: int) -> dict[str, Any]:
    value = _prompt_event(candidate)
    value["d1_assessment"] = {
        **dict(assessment),
        "assessment_score": _assessment_score(assessment),
        "shortlist_rank": int(rank),
    }
    return value


def _provider_envelope(value: Any) -> tuple[Any, dict[str, Any]]:
    if isinstance(value, StageDProviderCallResult):
        return value.parsed, {
            "raw_response": value.raw_response,
            "request_metadata": dict(value.request_metadata or {}),
        }
    return value, {
        "raw_response": None,
        "request_metadata": {},
    }


def _call_assessment_provider(
    ai_client: Any | None,
    events: Sequence[Mapping[str, Any]],
    *,
    edition: Mapping[str, Any],
    retries: int,
) -> tuple[Any, int, dict[str, Any]]:
    if ai_client is None or not callable(getattr(ai_client, "assess_events", None)):
        raise RuntimeError("Stage D assessment client is not configured")

    def operation() -> Any:
        value = ai_client.assess_events(events, edition=edition)
        parsed, _audit = _provider_envelope(value)
        if isinstance(parsed, StageDAssessmentResponse):
            return value
        return strict_parse_stage_d_assessment(parsed, event_ids=[int(event["event_id"]) for event in events])

    value, failure, attempts = call_with_provider_retries(
        operation,
        is_retryable=_provider_failure_is_retryable,
        stage="stage_d_assessment",
        max_retries=_bounded_int(retries, DEFAULT_STAGE_D_ASSESSMENT_RETRIES, lower=0, upper=5),
    )
    if failure is not None or value is None:
        cause = failure if failure is not None else RuntimeError("Stage D assessment returned no result")
        raise StageDProviderCallError(cause, attempts) from cause
    parsed, audit = _provider_envelope(value)
    return parsed, attempts, audit


def _call_composition_provider(
    ai_client: Any | None,
    events: Sequence[Mapping[str, Any]],
    *,
    edition: Mapping[str, Any],
    total_max: int,
    watchlist_max: int,
    retries: int,
) -> tuple[Any, int, dict[str, Any]]:
    if ai_client is None:
        raise RuntimeError("Stage D composition client is not configured")

    def operation() -> Any:
        method = getattr(ai_client, "compose_events", None)
        if not callable(method):
            raise RuntimeError("Stage D composition client is not configured")
        value = method(events, edition=edition, total_max=total_max, watchlist_max=watchlist_max)
        parsed, _audit = _provider_envelope(value)
        if isinstance(parsed, StageDCompositionResponse):
            return value
        if getattr(parsed, "decisions", None) is not None and not isinstance(parsed, Mapping):
            return value
        return strict_parse_stage_d_composition(
            parsed,
            event_ids=[int(event["event_id"]) for event in events],
            total_max=total_max,
            watchlist_max=watchlist_max,
            events=events,
        )

    value, failure, attempts = call_with_provider_retries(
        operation,
        is_retryable=_provider_failure_is_retryable,
        stage="stage_d_composition",
        max_retries=_bounded_int(retries, 2, lower=0, upper=5),
    )
    if failure is not None or value is None:
        cause = failure if failure is not None else RuntimeError("Stage D composition returned no result")
        raise StageDProviderCallError(cause, attempts) from cause
    parsed, audit = _provider_envelope(value)
    return parsed, attempts, audit


def _replace_stage_d_snapshot(
    repo: IntelRepository,
    *,
    run_id: int,
    candidates: Sequence[Mapping[str, Any]],
    gated_event_ids: set[int],
    assessments: Mapping[int, Mapping[str, Any]],
    shortlist_by_id: Mapping[int, Mapping[str, Any]],
    shortlist_rank: Mapping[int, int],
    decisions: Mapping[int, Mapping[str, Any]],
    policy: StageDProfile,
    result: StageDResult,
) -> None:
    repo.clear_event_stage_d_snapshot(run_id=run_id)
    selected_order = 0
    watchlist_order = 0
    for candidate in candidates:
        event = candidate["event"]
        event_id = int(event.id)
        assessment = assessments.get(event_id)
        if event_id in gated_event_ids:
            decision = _gated_decision(candidate)
            tier = "paper_gated"
        elif event_id not in shortlist_by_id:
            decision = _omitted_decision("not_shortlisted", "D1 评估后未进入 Stage D 短名单。", event_id=event_id)
            decision["editorial_score"] = round(_assessment_score(assessment or {}))
            tier = "omitted"
        else:
            decision = dict(decisions.get(event_id) or _omitted_decision("provider_missing_decision", "未获得可展示的编辑决策。", event_id=event_id))
            tier = str(decision.get("decision") or "omitted")
        if tier == "selected":
            selected_order += 1
            display_order = int(decision.get("display_order") or selected_order)
            result.selected += 1
        elif tier == "watchlist":
            watchlist_order += 1
            display_order = policy.total_max + watchlist_order
            result.watchlist += 1
        else:
            display_order = 0
            result.omitted += 1
        metadata = {
            "stage": STAGE_D_NAME,
            "stage_d_source": "ai" if event_id in decisions else "local",
            "stage_d_version": STAGE_D_VERSION,
            "profile_version": policy.version,
            "assessment": assessment,
            "assessment_score": _assessment_score(assessment or {}) if assessment is not None else None,
            "shortlist_rank": shortlist_rank.get(event_id),
            "editorial_tier": tier,
            "decision": decision.get("decision"),
            "paper_gate_pass": bool(candidate["paper_gate_pass"]),
            "paper_gate_reason": candidate["paper_gate_reason"],
            "source_evidence_level": candidate["source_evidence_level"],
            "community_source_group_count": candidate["community_source_group_count"],
            "source_presentation": _source_presentation(candidate),
            "editorial_score": decision.get("editorial_score"),
            "story_family_id": decision.get("story_family_id"),
            "family_position": decision.get("family_position"),
            "display_title_zh": decision.get("display_title_zh"),
            "title_supporting_fields": decision.get("title_supporting_fields", []),
            "reason_codes": decision.get("reason_codes", []),
            "editorial_reason": decision.get("editorial_reason"),
            "confidence": decision.get("confidence"),
            "watchlist_order": watchlist_order if tier == "watchlist" else None,
            "recent_daily_history": candidate["recent_daily_history"],
        }
        snapshot = repo.upsert_event_stage_d_snapshot(
            event_id,
            run_id=run_id,
            display_order=display_order,
            display_score=float(event.display_score or 0.0),
            selected=tier == "selected",
            topic=candidate["topic"],
            source_group=candidate["source_group"],
            content_class=candidate["content_class"],
            reason=(decision.get("reason_codes") or [candidate.get("paper_gate_reason") or "omitted"])[0],
            metadata=metadata,
        )
        result.snapshots += int(snapshot.created)


def _persist_stage_d_failure(session_factory: sessionmaker[Session], run_id: int, result: StageDResult) -> None:
    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            stage = repo.get_stage(int(run_id), STAGE_D_NAME)
            if stage is not None:
                repo.finish_stage(
                    stage,
                    status="failed",
                    error_category="stage",
                    error_code=f"stage_d_{result.failed_phase or 'failed'}",
                    error_message=(result.errors[-1] if result.errors else "Stage D failed")[-4000:],
                )
                session.commit()
    except Exception:
        LOGGER.exception("Unable to persist Stage D failure")


def _load_events(session: Session, *, run_id: int, event_ids: Iterable[int] | None) -> list[IntelEvent]:
    stmt = (
        select(IntelEvent)
        .options(
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.ai_review),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.item).joinedload(IntelItem.source),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source),
        )
        .where(IntelEvent.state.not_in(("rejected", "discarded", "filtered")))
        .order_by(IntelEvent.display_score.desc(), IntelEvent.event_key.asc(), IntelEvent.id.asc())
    )
    ids = _normalize_event_ids(event_ids or ())
    # Stage D may only consume the current Stage-C projection of this draft;
    # report history is read separately from DailyEditionReportEntry.
    stmt = stmt.where(
        IntelEvent.build_id == int(run_id),
        IntelEvent.id.in_(ids or [-1]),
    )
    return list(session.scalars(stmt).unique().all())


def _load_current_cluster_event_ids(session: Session, run_id: int) -> list[int]:
    stage = session.scalar(select(IntelRunStage).where(IntelRunStage.run_id == run_id, IntelRunStage.stage_name == "cluster"))
    if stage is None:
        return []
    task = session.scalar(
        select(IntelRunStageTask).where(
            IntelRunStageTask.stage_id == stage.id,
            IntelRunStageTask.subject_type == "run",
            IntelRunStageTask.subject_id == str(run_id),
        )
    )
    if task is None or task.status != "succeeded" or not isinstance(task.result, Mapping):
        return []
    return _normalize_event_ids(task.result.get("current_event_ids", task.result.get("event_ids", [])))


def _candidate(event: IntelEvent) -> dict[str, Any]:
    source_groups = _json_strings(event.source_groups_json)
    source_ids = _json_strings(event.source_ids_json)
    if not source_groups and event.source_group:
        source_groups = [event.source_group]
    community_groups: set[str] = set()
    community_items = 0
    trusted_items = 0
    for relation in event.event_items:
        if relation.source_id and str(relation.source_id) not in source_ids:
            source_ids.append(str(relation.source_id))
        if relation.source_group and str(relation.source_group) not in source_groups:
            source_groups.append(str(relation.source_group))
        if _relation_is_community(relation):
            community_items += 1
            if relation.source_group:
                community_groups.add(str(relation.source_group))
        else:
            trusted_items += 1
    community_only = community_items > 0 and trusted_items == 0
    community_group_count = len(community_groups)
    if community_only:
        source_evidence_level = "multi_community_signal" if community_group_count >= 2 else "single_community_signal"
    else:
        source_evidence_level = "trusted_or_first_party_supported"
    paper_gate_pass, paper_gate_reason = _paper_gate(event)
    return {
        "event": event,
        "topic": str(event.topic or "opinion").strip().casefold() or "opinion",
        "content_class": str(event.content_class or "").strip() or None,
        "source_group": event.source_group or (source_groups[0] if source_groups else None),
        "source_groups": tuple(dict.fromkeys(source_groups)),
        "source_ids": tuple(dict.fromkeys(source_ids)),
        "community_source_group_count": community_group_count,
        "source_evidence_level": source_evidence_level,
        "paper_gate_pass": paper_gate_pass,
        "paper_gate_reason": paper_gate_reason,
        "recent_daily_history": {"appeared_recently": False, "prior_editions": []},
    }


def _prompt_event(candidate: Mapping[str, Any]) -> dict[str, Any]:
    event = candidate["event"]
    return {
        "event_id": int(event.id),
        "title": str(event.title or ""),
        "summary_cn": str(event.summary_cn or event.title or ""),
        "topic": candidate["topic"],
        "keywords": _json_strings(event.keywords_json),
        "entities": event.entities,
        "published_at": _iso_datetime(event.last_seen_at or event.first_seen_at),
        "display_score": _number(event.display_score),
        "source_groups": list(candidate["source_groups"]),
        "source_ids": list(candidate["source_ids"]),
        "source_evidence_level": candidate["source_evidence_level"],
        "community_source_group_count": candidate["community_source_group_count"],
        "risk_flags": _json_strings(event.risk_flags_json),
        "resolution_method": event.resolution_method,
        "resolution_confidence": int(event.resolution_confidence or 0),
        "recent_daily_history": candidate["recent_daily_history"],
    }


def _recent_daily_history(
    session: Session,
    *,
    candidates: Sequence[Mapping[str, Any]],
    run: IntelRun,
    days: int,
) -> dict[int, dict[str, Any]]:
    if days <= 0 or run.edition_id is None or not run.edition_date:
        return {}
    try:
        current = date.fromisoformat(run.edition_date)
    except ValueError:
        return {}
    events = [candidate.get("event") for candidate in candidates if candidate.get("event") is not None]
    if not events:
        return {}
    earliest = current - timedelta(days=days)
    rows = session.execute(
        select(DailyEditionReportEntry, DailyEdition)
        .join(DailyEdition, DailyEdition.id == DailyEditionReportEntry.edition_id)
        .where(
            DailyEdition.edition_date >= earliest,
            DailyEdition.edition_date < current,
            DailyEdition.published_at.is_not(None),
        )
        .order_by(DailyEdition.edition_date.desc(), DailyEditionReportEntry.display_order.asc())
    ).all()
    entry_keys = [(_published_entry_identity_keys(entry), edition.edition_date.isoformat()) for entry, edition in rows]
    history: dict[int, list[str]] = {}
    for event in events:
        event_id = int(event.id)
        event_keys = _event_history_identity_keys(event)
        if not event_keys:
            continue
        editions = [edition_date for entry_identity, edition_date in entry_keys if event_keys & entry_identity]
        if editions:
            history[event_id] = list(dict.fromkeys(editions))
    return {
        event_id: {"appeared_recently": True, "prior_editions": editions}
        for event_id, editions in history.items()
    }
def _event_history_identity_keys(event: IntelEvent) -> set[str]:
    keys = {_history_identity("event", event.event_key)}
    if event.event_key and str(event.event_key).startswith(("url:", "external:")):
        keys.add(_history_identity("stable", event.event_key))
    if event.canonical_url:
        keys.add(_history_identity("url", event.canonical_url))
    if event.external_id:
        keys.add(_history_identity("external", event.external_id))
    return {value for value in keys if value}


def _published_entry_identity_keys(entry: DailyEditionReportEntry) -> set[str]:
    keys = {_history_identity("event", entry.event_key)}
    if entry.event_key and str(entry.event_key).startswith(("url:", "external:")):
        keys.add(_history_identity("stable", entry.event_key))
    if entry.url:
        keys.add(_history_identity("url", entry.url))
    return {value for value in keys if value}


def _history_identity(kind: str, value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if kind == "url":
        text = text.rstrip("/")
    return f"{kind}:{text}"


def _provider_failure_is_retryable(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    try:
        if status_code is not None:
            return int(status_code) == 429 or int(status_code) >= 500
    except (TypeError, ValueError):
        pass
    name = exc.__class__.__name__.casefold()
    return any(token in name for token in ("timeout", "connect", "network", "transport"))


def _clear_provider_audit(ai_client: Any | None) -> None:
    if ai_client is None:
        return
    for name in ("last_raw_response", "last_request_metadata", "last_error_metadata"):
        if hasattr(ai_client, name):
            try:
                setattr(ai_client, name, None)
            except (AttributeError, TypeError):
                continue


def _provider_audit(exc: BaseException, ai_client: Any | None) -> dict[str, Any]:
    raw_response = getattr(exc, "raw_response", None)
    if raw_response is None:
        raw_response = getattr(ai_client, "last_raw_response", None)
    request_metadata = getattr(exc, "request_metadata", None)
    if not isinstance(request_metadata, Mapping):
        request_metadata = getattr(ai_client, "last_request_metadata", None)
    return {
        "provider_attempts": int(getattr(exc, "provider_attempts", 0) or 0),
        "status_code": getattr(exc, "status_code", None),
        "error_code": getattr(exc, "error_code", None),
        "error_message": getattr(exc, "error_message", None),
        "raw_response": raw_response,
        "request_metadata": dict(request_metadata or {}) if isinstance(request_metadata, Mapping) else {},
    }


def _gated_decision(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return _omitted_decision(
        str(candidate.get("paper_gate_reason") or "paper_gate:unsupported"),
        "论文未通过本地证据门槛，未进入编辑选择池。",
        event_id=int(candidate["event"].id),
    )


def _omitted_decision(reason_code: str, reason: str, *, event_id: int | None = None) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "decision": "omitted",
        "display_order": None,
        "editorial_score": 0,
        "story_family_id": f"omitted_{event_id or 'unknown'}",
        "family_position": None,
        "display_title_zh": None,
        "title_supporting_fields": [],
        "reason_codes": [reason_code],
        "editorial_reason": reason,
        "confidence": 0,
    }


def _source_presentation(candidate: Mapping[str, Any]) -> str | None:
    level = candidate.get("source_evidence_level")
    if level == "single_community_signal":
        return "community_signal_pending_verification"
    if level == "multi_community_signal":
        return "multi_community_signal_pending_verification"
    return None


def _stage_d_input_fingerprint(candidates: Sequence[Mapping[str, Any]], policy: StageDProfile) -> str:
    payload = {
        "profile": {"version": policy.version, "total_max": policy.total_max, "paper_hard_gate": policy.paper_hard_gate},
        "events": [
            {
                "id": int(candidate["event"].id),
                "title": candidate["event"].title,
                "summary_cn": candidate["event"].summary_cn,
                "display_score": candidate["event"].display_score,
                "topic": candidate["event"].topic,
                "keywords": candidate["event"].keywords_json,
                "entities": candidate["event"].entities_json,
                "risk_flags": candidate["event"].risk_flags_json,
                "last_seen_at": _iso_datetime(candidate["event"].last_seen_at),
            }
            for candidate in candidates
        ],
    }
    return _response_hash(payload)


def _paper_gate(event: IntelEvent) -> tuple[bool, str | None]:
    if str(event.topic or "").casefold() != "paper":
        return True, None
    flags = set(_json_strings(event.risk_flags_json))
    if "paper:arxiv_only" in flags or (event.canonical_url and "arxiv.org" in event.canonical_url.casefold()):
        return False, "paper_gate:arxiv_only"
    if any(flag in flags for flag in ("paper:unsupported", "paper:not_declared")):
        return False, "paper_gate:unsupported"
    supports: list[Mapping[str, Any]] = []
    event_raw = _json_value(event.resolution_raw_json, {})
    if isinstance(event_raw, Mapping):
        for key in ("paper_support", "paper", "paper_evidence"):
            if isinstance(event_raw.get(key), Mapping):
                supports.append(event_raw[key])
    for relation in event.event_items:
        review = relation.item.ai_review if relation.item is not None else None
        support = _json_value(getattr(review, "paper_support_json", None), {}) if review is not None else {}
        if isinstance(support, Mapping) and support:
            supports.append(support)
        raw = _json_value(getattr(review, "raw_response_json", None), {}) if review is not None else {}
        if isinstance(raw, Mapping):
            support = raw.get("paper_support", raw.get("paper", raw.get("paper_evidence")))
            if isinstance(support, Mapping):
                supports.append(support)
    if any(_paper_support_passes(value) for value in supports):
        return True, None
    return False, "paper_gate:unsupported"


def _paper_support_passes(support: Mapping[str, Any]) -> bool:
    if not isinstance(support, Mapping) or _coerce_bool(support.get("arxiv_only", False), False):
        return False
    if support.get("is_paper") is False or support.get("supported") is False:
        return False
    level = str(support.get("support_level", "")).strip().casefold()
    if level and level not in {"supported", "strong", "pass", "true"}:
        return False
    if support.get("hard_gate_pass") is not None:
        return _coerce_bool(support.get("hard_gate_pass"), False)
    links = [str(value) for value in support.get("evidence_links", []) if value] if isinstance(support.get("evidence_links"), list) else []
    evidence = [support.get(key) for key in ("evidence_url", "official_url", "code_url", "github_url")] + links
    return bool(_coerce_bool(support.get("has_official_source"), False) or _coerce_bool(support.get("has_code"), False) or any(str(value or "").strip() and "arxiv.org" not in str(value).casefold() for value in evidence))


def _relation_is_community(relation: IntelEventItem) -> bool:
    item = relation.item
    source = relation.source or (item.source if item is not None else None)
    if source is not None and is_first_party_x_source(source):
        return False
    review = item.ai_review if item is not None else None
    flags = set(review.risk_flags if review is not None else [])
    content_class = str((review.content_class if review is not None else None) or (item.content_class if item is not None else None) or "").strip()
    return "source:social_only" in flags or content_class == "community_social"


def _normalize_event_ids(value: Iterable[Any] | Any) -> list[int]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        return []
    result: list[int] = []
    for raw in value:
        try:
            event_id = int(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if event_id > 0 and event_id not in result:
            result.append(event_id)
    return result


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _json_strings(value: Any) -> list[str]:
    raw = _json_value(value, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Iterable) or isinstance(raw, Mapping):
        return []
    result: list[str] = []
    for item in raw:
        text = str(item).strip() if item is not None else ""
        if text and text not in result:
            result.append(text)
    return result


def _response_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iso_datetime(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") and value is not None else None


def _number(value: Any) -> float:
    try:
        return max(0.0, float(str(value).replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _bounded_int(value: Any, default: int, *, lower: int, upper: int) -> int:
    try:
        return max(lower, min(upper, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"true", "1", "yes", "y", "on"}:
            return True
        if text in {"false", "0", "no", "n", "off"}:
            return False
    return default


__all__ = [
    "STAGE_D_NAME",
    "StageDExecutionError",
    "StageDProfile",
    "StageDResult",
    "load_stage_d_profile",
    "run_stage_d_from_settings",
    "run_stage_d_job",
]
