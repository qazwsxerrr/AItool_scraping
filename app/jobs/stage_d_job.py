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
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.skills.stage_d_editorial import StageDEditorialClient, StageDEditorialResponse, strict_parse_stage_d
from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.config.settings import Settings
from app.domain.policies import is_first_party_x_source
from app.jobs.provider_retry import call_with_provider_retries
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    IntelEvent,
    IntelEventItem,
    IntelEventStageDSnapshot,
    IntelItem,
    IntelRun,
    IntelRunStage,
    IntelRunStageTask,
)
from app.storage.repository import IntelRepository


LOGGER = logging.getLogger(__name__)
STAGE_D_NAME = "stage_d"


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
    """Small policy surface deliberately free of editorial quotas."""

    snapshot_key: str = "latest"
    total_max: int = DEFAULT_DAILY_REPORT_LIMIT
    paper_hard_gate: bool = True
    recent_history_days: int = 3
    version: str = "stage-d-v1"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "StageDProfile":
        data = dict(value or {})
        paper = data.get("paper") if isinstance(data.get("paper"), Mapping) else {}
        return cls(
            snapshot_key=str(data.get("snapshot_key") or "latest"),
            total_max=_bounded_int(data.get("total_max"), DEFAULT_DAILY_REPORT_LIMIT, lower=0, upper=30),
            paper_hard_gate=_coerce_bool(data.get("paper_hard_gate", paper.get("hard_gate", True)), True),
            recent_history_days=_bounded_int(data.get("recent_history_days"), 3, lower=0, upper=30),
            version=str(data.get("version") or "stage-d-v1"),
        )


@dataclass
class StageDResult:
    run_id: int | None = None
    snapshot_key: str = "latest"
    processed: int = 0
    eligible: int = 0
    selected: int = 0
    omitted: int = 0
    paper_gated: int = 0
    snapshots: int = 0
    ai_selected: int = 0
    ai_failed: int = 0
    provider_attempts: int = 0
    used_fallback: bool = False
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
    snapshot_key: str | None = None,
    run_id: int | None = None,
    event_ids: Iterable[int] | None = None,
) -> StageDResult:
    """Select a final daily edition from this run's Stage-C projection."""

    policy = _coerce_profile(profile if profile is not None else profile_path)
    result = StageDResult(run_id=run_id, snapshot_key=str(snapshot_key or policy.snapshot_key))
    owner = "stage-d-editorial"
    stage = None
    stage_task = None
    with session_factory() as session:
        try:
            repo = IntelRepository(session)
            if run_id is None and event_ids is None:
                latest_run_id = session.scalar(select(func.max(IntelRun.id)))
                if latest_run_id is not None:
                    run_id = int(latest_run_id)
                    result.run_id = run_id
            run = session.get(IntelRun, int(run_id)) if run_id is not None else None
            key = str(snapshot_key or (run.daily_snapshot_key if run is not None else None) or policy.snapshot_key)
            result.snapshot_key = key
            if run is not None:
                stage = repo.ensure_stage(
                    int(run_id),
                    STAGE_D_NAME,
                    metadata={"snapshot_key": key, "profile_version": policy.version, "prompt_version": "stage_d_editorial_v1"},
                )
            if run_id is not None and event_ids is None:
                event_ids = _load_current_cluster_event_ids(session, int(run_id))
            events = _load_events(session, run_id=run_id, event_ids=event_ids)
            result.processed = len(events)
            candidates = [_candidate(event) for event in events]
            history = _recent_daily_history(session, candidates=candidates, run=run, days=policy.recent_history_days)
            for candidate in candidates:
                candidate["recent_daily_history"] = history.get(int(candidate["event"].id), {"appeared_recently": False, "prior_editions": []})

            eligible = [candidate for candidate in candidates if candidate["paper_gate_pass"] or not policy.paper_hard_gate]
            gated = [candidate for candidate in candidates if not (candidate["paper_gate_pass"] or not policy.paper_hard_gate)]
            gated_event_ids = {int(candidate["event"].id) for candidate in gated}
            result.eligible = len(eligible)
            result.paper_gated = len(gated)

            input_fingerprint = _stage_d_input_fingerprint(candidates, policy, key)
            config_fingerprint = f"stage-d-v1:{policy.version}:stage_d_editorial_v1:{getattr(ai_client, 'model', None) or 'unconfigured'}"
            if stage is not None:
                stage_task = repo.ensure_stage_task(
                    stage,
                    subject_type="run",
                    subject_id=int(run_id),
                    target_run_id=int(run_id),
                    input_fingerprint=input_fingerprint,
                    config_fingerprint=config_fingerprint,
                )
                claimed = repo.claim_stage_task(
                    stage,
                    task_id=stage_task.id,
                    owner=owner,
                    force=True if force or stage_task.status == "succeeded" else False,
                    input_fingerprint=input_fingerprint,
                    config_fingerprint=config_fingerprint,
                )
                if claimed is None:
                    result.errors.append("stage_d is already running")
                    return result
                stage_task = claimed
                session.commit()

            payload = [_prompt_event(candidate) for candidate in eligible]
            decisions: dict[int, dict[str, Any]] = {}
            stage_d_source = "ai"
            response_hash: str | None = None
            fallback_reason: str | None = None
            provider_audit: dict[str, Any] = {}
            if payload:
                _clear_provider_audit(ai_client)
                try:
                    response, attempts = _call_editorial_provider(
                        ai_client,
                        payload,
                        edition={
                            "date": run.edition_date if run is not None else None,
                            "max_selected": policy.total_max,
                            "max_selected_per_story_family": 2,
                        },
                        total_max=policy.total_max,
                        retries=getattr(
                            ai_client,
                            "max_retries",
                            getattr(getattr(ai_client, "settings", None), "ai_stage_d_retries", None),
                        ),
                    )
                    result.provider_attempts = attempts
                    decisions = {decision.event_id: decision.model_dump(mode="json") for decision in response.decisions}
                    raw_response = getattr(ai_client, "last_raw_response", None)
                    response_hash = _response_hash(raw_response if raw_response is not None else response.model_dump(mode="json"))
                    result.ai_selected = sum(1 for value in decisions.values() if value["decision"] == "selected")
                except Exception as exc:
                    result.ai_failed = 1
                    result.used_fallback = True
                    fallback_reason = str(exc)
                    result.errors.append(fallback_reason)
                    stage_d_source = "deterministic_fallback"
                    provider_audit = _provider_audit(exc, ai_client)
                    result.provider_attempts = max(result.provider_attempts, int(provider_audit.get("provider_attempts") or 0))
                    raw_response = provider_audit.get("raw_response")
                    if raw_response is not None:
                        response_hash = _response_hash(raw_response)
                    LOGGER.warning("Stage D provider failed; using deterministic fallback: %s", exc)
                    decisions = _fallback_decisions(eligible, total_max=policy.total_max)
            else:
                # An empty eligible pool is an auditable empty edition, not a
                # provider failure and not an invitation to fill with gated papers.
                stage_d_source = "no_eligible_events"
            if not provider_audit:
                request_metadata = getattr(ai_client, "last_request_metadata", None)
                provider_audit = {
                    "provider_attempts": result.provider_attempts,
                    "status_code": None,
                    "error_code": None,
                    "error_message": None,
                    "raw_response": getattr(ai_client, "last_raw_response", None),
                    "request_metadata": dict(request_metadata) if isinstance(request_metadata, Mapping) else {},
                }

            repo.clear_event_stage_d_snapshot(snapshot_key=key)
            for candidate in candidates:
                event = candidate["event"]
                event_id = int(event.id)
                decision = decisions.get(event_id)
                if event_id in gated_event_ids:
                    decision = _gated_decision(candidate)
                elif decision is None:
                    # This can only occur after a provider defect.  Do not
                    # fill an AI edition locally; make the omission visible.
                    decision = _omitted_decision("provider_missing_decision", "未获得可展示的编辑决策。")
                selected = decision["decision"] == "selected"
                if selected:
                    result.selected += 1
                else:
                    result.omitted += 1
                metadata = {
                    "stage": STAGE_D_NAME,
                    "stage_d_source": stage_d_source,
                    "profile_version": policy.version,
                    "prompt_version": "stage_d_editorial_v1",
                    "paper_gate_pass": bool(candidate["paper_gate_pass"]),
                    "paper_gate_reason": candidate["paper_gate_reason"],
                    "source_evidence_level": candidate["source_evidence_level"],
                    "community_source_group_count": candidate["community_source_group_count"],
                    "source_presentation": _source_presentation(candidate),
                    "decision": decision["decision"],
                    "editorial_score": decision.get("editorial_score"),
                    "story_family_id": decision.get("story_family_id"),
                    "family_position": decision.get("family_position"),
                    "display_title_zh": decision.get("display_title_zh"),
                    "title_supporting_fields": decision.get("title_supporting_fields", []),
                    "reason_codes": decision.get("reason_codes", []),
                    "editorial_reason": decision.get("editorial_reason"),
                    "confidence": decision.get("confidence"),
                    "fallback_rank": decision.get("fallback_rank"),
                    "fallback_score_components": decision.get("fallback_score_components"),
                    "recent_daily_history": candidate["recent_daily_history"],
                    "provider_attempts": result.provider_attempts,
                    "response_hash": response_hash,
                    "fallback_reason": fallback_reason,
                    "provider_status_code": provider_audit.get("status_code"),
                    "provider_error_code": provider_audit.get("error_code"),
                    "provider_error_message": provider_audit.get("error_message"),
                }
                snapshot = repo.upsert_event_stage_d_snapshot(
                    event_id,
                    snapshot_key=key,
                    run_id=run_id,
                    display_order=int(decision.get("display_order") or 0),
                    display_score=float(event.display_score or 0.0),
                    selected=selected,
                    topic=candidate["topic"],
                    source_group=candidate["source_group"],
                    content_class=candidate["content_class"],
                    reason=(decision.get("reason_codes") or [candidate.get("paper_gate_reason") or "omitted"])[0],
                    metadata=metadata,
                )
                result.snapshots += int(snapshot.created)
            session.commit()
            if stage_task is not None:
                stage_metadata = {
                    "stage_d_source": stage_d_source,
                    "provider_attempts": result.provider_attempts,
                    "fallback_reason": fallback_reason,
                    "response_hash": response_hash,
                    "provider_status_code": provider_audit.get("status_code"),
                    "provider_error_code": provider_audit.get("error_code"),
                    "provider_error_message": provider_audit.get("error_message"),
                    "request_metadata": provider_audit.get("request_metadata") or {},
                }
                repo.complete_stage_task(
                    stage_task,
                    owner=owner,
                    result_ref={"projection": "IntelEventStageDSnapshot", "snapshot_key": key},
                    result={
                        "processed": result.processed,
                        "eligible": result.eligible,
                        "selected": result.selected,
                        "paper_gated": result.paper_gated,
                        "stage_d_source": stage_d_source,
                        "response_hash": response_hash,
                        "fallback_reason": fallback_reason,
                        "provider_attempts": result.provider_attempts,
                        "provider_error": {
                            key: provider_audit.get(key)
                            for key in ("status_code", "error_code", "error_message")
                            if provider_audit.get(key) is not None
                        },
                    },
                    raw_response=provider_audit.get("raw_response") or getattr(ai_client, "last_raw_response", None),
                    metadata=stage_metadata,
                )
                repo.finish_stage(stage, status="succeeded", metadata=stage_metadata, owner=owner)
                session.commit()
        except Exception as exc:
            session.rollback()
            result.errors.append(str(exc))
            LOGGER.exception("Stage D failed")
            if stage is not None and stage_task is not None:
                try:
                    repo = IntelRepository(session)
                    task = repo.get_task(stage, subject_type="run", subject_id=int(run_id))
                    if task is not None and task.status == "running":
                        repo.fail_stage_task(
                            task,
                            owner=owner,
                            error_category="stage",
                            error_code="stage_d_failed",
                            error_message=str(exc),
                            retryable=True,
                        )
                        session.commit()
                except Exception:
                    LOGGER.exception("Unable to persist Stage D failure")
    return result


def run_stage_d_from_settings(
    *,
    settings: Settings,
    profile: StageDProfile | Mapping[str, Any] | str | Path | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    force: bool = False,
    snapshot_key: str | None = None,
    run_id: int | None = None,
    event_ids: Iterable[int] | None = None,
) -> StageDResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return run_stage_d_job(
        session_factory=create_session_factory(engine),
        profile=profile if profile is not None else profile_path,
        ai_client=ai_client if ai_client is not None else StageDEditorialClient.from_settings(settings),
        force=force,
        snapshot_key=snapshot_key,
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


def _load_events(session: Session, *, run_id: int | None, event_ids: Iterable[int] | None) -> list[IntelEvent]:
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
    if event_ids is not None:
        ids = _normalize_event_ids(event_ids)
        stmt = stmt.where(IntelEvent.id.in_(ids or [-1]))
    elif run_id is not None:
        # A run must consume an explicit Stage-C current projection; it must
        # never fall back to all historical events.
        stmt = stmt.where(IntelEvent.id.in_([-1]))
    else:
        latest_run = session.scalar(select(func.max(IntelRun.id)))
        stmt = stmt.where(IntelEvent.new_in_run_id == latest_run if latest_run else IntelEvent.new_in_run_id.is_(None))
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
    run: IntelRun | None,
    days: int,
) -> dict[int, dict[str, Any]]:
    if run is None or days <= 0 or not run.edition_date:
        return {}
    try:
        current = date.fromisoformat(run.edition_date)
    except ValueError:
        return {}
    event_ids = [int(candidate["event"].id) for candidate in candidates]
    if not event_ids:
        return {}
    earliest = current - timedelta(days=days)
    previous_runs = list(
        session.scalars(
            select(IntelRun)
            .where(
                IntelRun.status.in_(("completed", "completed_with_errors", "partial")),
                IntelRun._edition_date >= earliest,
                IntelRun._edition_date < current,
            )
            .order_by(IntelRun._edition_date.desc(), IntelRun.id.desc())
        ).all()
    )
    # A date can have multiple internal runs. Compare only against the newest
    # date-addressed edition, because that is the prior day's final output.
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
    if not conditions:
        return {}
    rows = session.execute(
        select(IntelEventStageDSnapshot, IntelRun)
        .join(IntelRun, IntelRun.id == IntelEventStageDSnapshot.run_id)
        .where(
            IntelEventStageDSnapshot.event_id.in_(event_ids),
            IntelEventStageDSnapshot.selected.is_(True),
            or_(*conditions),
        )
        .order_by(IntelRun._edition_date.desc(), IntelEventStageDSnapshot.updated_at.desc())
    ).all()
    history: dict[int, list[str]] = {}
    for snapshot, previous_run in rows:
        history.setdefault(int(snapshot.event_id), [])
        if previous_run.edition_date and previous_run.edition_date not in history[int(snapshot.event_id)]:
            history[int(snapshot.event_id)].append(previous_run.edition_date)
    return {
        event_id: {"appeared_recently": True, "prior_editions": editions}
        for event_id, editions in history.items()
    }


def _call_editorial_provider(
    ai_client: Any | None,
    events: Sequence[Mapping[str, Any]],
    *,
    edition: Mapping[str, Any],
    total_max: int,
    retries: Any,
) -> tuple[StageDEditorialResponse, int]:
    if ai_client is None:
        raise RuntimeError("Stage D editorial client is not configured")

    def operation() -> StageDEditorialResponse:
        method = getattr(ai_client, "select_events", None)
        if not callable(method):
            method = getattr(ai_client, "stage_d_editorial", None)
        if not callable(method):
            method = getattr(ai_client, "editorial_select", None)
        if not callable(method):
            raise TypeError("Stage D client does not expose select_events")
        value = method(events, edition=edition, total_max=total_max)
        if isinstance(value, StageDEditorialResponse):
            return strict_parse_stage_d(
                value.model_dump(mode="json"),
                event_ids=[int(item["event_id"]) for item in events],
                total_max=total_max,
                events=events,
            )
        return strict_parse_stage_d(
            value,
            event_ids=[int(item["event_id"]) for item in events],
            total_max=total_max,
            events=events,
        )

    value, failure, attempts = call_with_provider_retries(
        operation,
        is_retryable=_provider_failure_is_retryable,
        stage="stage_d",
        max_retries=_bounded_int(retries, 2, lower=0, upper=5),
    )
    if failure is not None or value is None:
        cause = failure if failure is not None else RuntimeError("Stage D provider returned no result")
        raise StageDProviderCallError(cause, attempts) from cause
    return value, attempts


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


_FALLBACK_SOURCE_PENALTY_STEP = 6
_FALLBACK_SOURCE_PENALTY_MAX = 12
_FALLBACK_CONTENT_PENALTY_STEP = 4
_FALLBACK_CONTENT_PENALTY_MAX = 8
_FALLBACK_TOPIC_PENALTY_STEP = 3
_FALLBACK_TOPIC_PENALTY_MAX = 6
_FALLBACK_ENTITY_PENALTY_STEP = 4
_FALLBACK_ENTITY_PENALTY_MAX = 8
_FALLBACK_STORY_PENALTY_STEP = 4
_FALLBACK_STORY_PENALTY_MAX = 8
_FALLBACK_TRUSTED_BONUS = 3


def _fallback_decisions(candidates: Sequence[Mapping[str, Any]], *, total_max: int) -> dict[int, dict[str, Any]]:
    """Choose a deterministic fallback edition with bounded soft diversity signals."""

    remaining = list(candidates)
    selected_context: list[dict[str, str | None]] = []
    decisions: dict[int, dict[str, Any]] = {}
    selected_count = 0
    rank = 0
    limit = max(0, int(total_max))
    while remaining:
        scored: list[tuple[dict[str, Any], Mapping[str, Any], str | None, int]] = []
        for index, candidate in enumerate(remaining):
            event = candidate["event"]
            components = _fallback_score_components(candidate, selected_context)
            title = _fallback_title(event, community_signal=_source_presentation(candidate) is not None)
            scored.append((components, candidate, title, index))
        scored.sort(
            key=lambda row: (
                -float(row[0]["adjusted"]),
                -float(row[0]["base"]),
                int(row[1]["event"].id),
            )
        )
        components, candidate, title, index = scored[0]
        remaining.pop(index)
        event = candidate["event"]
        event_id = int(event.id)
        rank += 1
        reason_codes = _fallback_reason_codes(components)
        if title is not None and selected_count < limit:
            selected_count += 1
            selected_context.append(_fallback_signal_keys(candidate))
            decisions[event_id] = {
                "event_id": event_id,
                "decision": "selected",
                "display_order": selected_count,
                "editorial_score": round(float(components["base"])),
                "story_family_id": f"fallback_{event_id}",
                "family_position": 1,
                "display_title_zh": title,
                "title_supporting_fields": ["summary_cn", "title"],
                "reason_codes": reason_codes,
                "editorial_reason": "编辑服务不可用，按 display_score 主导的确定性软多样性回退排序。",
                "confidence": 0,
                "fallback_rank": rank,
                "fallback_score_components": components,
            }
            continue

        if title is None:
            reason_codes.append("title_unavailable")
            reason = "编辑服务不可用，候选标题不可用，未进入回退展示列表。"
        else:
            reason_codes.append("fallback_limit")
            reason = "编辑服务不可用，已达到回退展示上限。"
        decisions[event_id] = {
            **_omitted_decision(reason_codes[-1], reason, event_id=event_id),
            "editorial_score": round(float(components["base"])),
            "reason_codes": reason_codes,
            "fallback_rank": rank,
            "fallback_score_components": components,
        }
    return decisions


def _fallback_score_components(
    candidate: Mapping[str, Any],
    selected_context: Sequence[Mapping[str, str | None]],
) -> dict[str, float | int]:
    keys = _fallback_signal_keys(candidate)
    counts = {
        "source_group": _fallback_repeat_count(keys.get("source_group"), selected_context, "source_group"),
        "content_class": _fallback_repeat_count(keys.get("content_class"), selected_context, "content_class"),
        "topic": _fallback_repeat_count(keys.get("topic"), selected_context, "topic"),
        "primary_entity": _fallback_repeat_count(keys.get("primary_entity"), selected_context, "primary_entity"),
        "story": _fallback_repeat_count(keys.get("story"), selected_context, "story"),
    }
    source_penalty = min(_FALLBACK_SOURCE_PENALTY_MAX, counts["source_group"] * _FALLBACK_SOURCE_PENALTY_STEP)
    content_penalty = min(_FALLBACK_CONTENT_PENALTY_MAX, counts["content_class"] * _FALLBACK_CONTENT_PENALTY_STEP)
    topic_penalty = min(_FALLBACK_TOPIC_PENALTY_MAX, counts["topic"] * _FALLBACK_TOPIC_PENALTY_STEP)
    entity_penalty = min(_FALLBACK_ENTITY_PENALTY_MAX, counts["primary_entity"] * _FALLBACK_ENTITY_PENALTY_STEP)
    story_penalty = min(_FALLBACK_STORY_PENALTY_MAX, counts["story"] * _FALLBACK_STORY_PENALTY_STEP)
    bonus = _fallback_trusted_bonus(candidate)
    base = _number(candidate["event"].display_score)
    adjusted = base + bonus - source_penalty - content_penalty - topic_penalty - entity_penalty - story_penalty
    return {
        "base": round(base, 4),
        "bonus": bonus,
        "same_source_group_penalty": source_penalty,
        "same_content_class_penalty": content_penalty,
        "same_topic_penalty": topic_penalty,
        "same_primary_entity_penalty": entity_penalty,
        "same_story_penalty": story_penalty,
        "adjusted": round(adjusted, 4),
    }


def _fallback_reason_codes(components: Mapping[str, Any]) -> list[str]:
    codes = ["deterministic_fallback"]
    for key, code in (
        ("same_source_group_penalty", "fallback_repeat_source_group"),
        ("same_content_class_penalty", "fallback_repeat_content_class"),
        ("same_topic_penalty", "fallback_repeat_topic"),
        ("same_primary_entity_penalty", "fallback_repeat_primary_entity"),
        ("same_story_penalty", "fallback_repeat_story"),
    ):
        if _number(components.get(key)) > 0:
            codes.append(code)
    if _number(components.get("bonus")) > 0:
        codes.append("fallback_trusted_evidence_bonus")
    return codes


def _fallback_repeat_count(
    value: str | None,
    selected_context: Sequence[Mapping[str, str | None]],
    key: str,
) -> int:
    if not value:
        return 0
    return sum(1 for selected in selected_context if selected.get(key) == value)


def _fallback_signal_keys(candidate: Mapping[str, Any]) -> dict[str, str | None]:
    event = candidate["event"]
    source_groups = candidate.get("source_groups")
    if not source_groups:
        source_groups = _json_strings(getattr(event, "source_groups_json", None))
    if isinstance(source_groups, str):
        source_groups = [source_groups]
    source_group = candidate.get("source_group") or next((str(value) for value in source_groups or [] if value), None)
    content_class = candidate.get("content_class") or getattr(event, "content_class", None)
    topic = candidate.get("topic") or getattr(event, "topic", None)
    return {
        "source_group": _fallback_token(source_group),
        "content_class": _fallback_token(content_class),
        "topic": _fallback_token(topic),
        "primary_entity": _fallback_primary_entity(candidate),
        "story": _fallback_story_key(candidate),
    }


def _fallback_primary_entity(candidate: Mapping[str, Any]) -> str | None:
    explicit = candidate.get("primary_entity") or candidate.get("primary_entity_name")
    if explicit:
        return _fallback_token(explicit)
    event = candidate["event"]
    entities = candidate.get("entities")
    if not entities:
        entities = getattr(event, "entities", None)
    if not entities:
        entities = _json_value(getattr(event, "entities_json", None), [])
    if not isinstance(entities, (list, tuple)):
        return None
    for entity in entities:
        if isinstance(entity, Mapping):
            value = entity.get("name") or entity.get("text") or entity.get("value") or entity.get("entity")
        else:
            value = entity
        token = _fallback_token(value)
        if token:
            return token
    return None


def _fallback_story_key(candidate: Mapping[str, Any]) -> str | None:
    event = candidate["event"]
    for value in (
        candidate.get("story_family_id"),
        candidate.get("story_key"),
        getattr(event, "story_family_id", None),
        getattr(event, "story_key", None),
    ):
        token = _fallback_token(value)
        if token:
            return token
    return None


def _fallback_trusted_bonus(candidate: Mapping[str, Any]) -> int:
    evidence = _fallback_token(candidate.get("source_evidence_level"))
    if evidence != "trusted_or_first_party_supported":
        return 0
    event = candidate["event"]
    groups = candidate.get("source_groups") or _json_strings(getattr(event, "source_groups_json", None))
    if not groups and candidate.get("source_group"):
        groups = (candidate.get("source_group"),)
    if isinstance(groups, str):
        groups = (groups,)
    content_class = _fallback_token(candidate.get("content_class") or getattr(candidate["event"], "content_class", None)) or ""
    trusted_group = any(
        (token := _fallback_token(group))
        and (token.startswith("official_") or token.endswith("_official") or token in {"official", "vendor_docs", "research"})
        for group in groups
    )
    if trusted_group or content_class.startswith("official_") or content_class in {"news_media", "academic_paper"}:
        return _FALLBACK_TRUSTED_BONUS
    return 0


def _fallback_token(value: Any) -> str | None:
    text = " ".join(str(value or "").strip().casefold().split())
    return text or None


def _fallback_title(event: IntelEvent, *, community_signal: bool = False) -> str | None:
    for raw in (event.summary_cn, event.title):
        text = str(raw or "").strip().replace("\n", " ").replace("\r", " ")
        if not text:
            continue
        text = text[:36].strip(" ，。；;:：-—")
        if (
            8 <= len(text) <= 36
            and not any(word in text for word in ("重磅", "颠覆", "史上最强", "最强", "革命性"))
            and not any(token in text.casefold() for token in ("http://", "https://", "www.", "`", "[", "]", "<", ">"))
        ):
            if community_signal and not any(cue in text for cue in ("社区", "传闻", "据称", "报道称", "消息称", "待核实", "爆料")):
                text = ("社区称：" + text)[:36].strip(" ，。；;:：-—")
            return text if 8 <= len(text) <= 36 else None
    return None


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


def _stage_d_input_fingerprint(candidates: Sequence[Mapping[str, Any]], policy: StageDProfile, snapshot_key: str) -> str:
    payload = {
        "snapshot_key": snapshot_key,
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


__all__ = ["STAGE_D_NAME", "StageDProfile", "StageDResult", "load_stage_d_profile", "run_stage_d_from_settings", "run_stage_d_job"]
