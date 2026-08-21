"""Stage D: select an ordered subset from the Stage-C candidate event pool."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.skills.stage_d_selection import (
    STAGE_D_SELECTION_PROMPT_VERSION,
    STAGE_D_SELECTION_SCHEMA_VERSION,
    StageDSelectionCallResult,
    StageDSelectionClient,
    StageDSelectionProviderError,
    StageDSelectionResponse,
    build_stage_d_provider_payload,
    strict_parse_stage_d_selection,
)
from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelRun, IntelRunStageTask
from app.storage.repository import IntelRepository


LOGGER = logging.getLogger(__name__)
STAGE_D_NAME = "stage_d"
STAGE_D_VERSION = "stage-d-selection-v1"


class StageDExecutionError(RuntimeError):
    """Stage-D failure that prevents export of the current draft."""

    def __init__(self, phase: str, message: str, *, cause: BaseException | None = None) -> None:
        self.phase = str(phase)
        self.cause = cause
        super().__init__(f"stage_d {self.phase} failed: {message}")


@dataclass(frozen=True)
class StageDProfile:
    """The only Stage-D policy: how many Stage-C candidates may be selected."""

    max_selected: int = DEFAULT_DAILY_REPORT_LIMIT
    version: str = STAGE_D_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "StageDProfile":
        data = dict(value or {})
        return cls(
            max_selected=_bounded_int(
                data.get("max_selected"),
                DEFAULT_DAILY_REPORT_LIMIT,
                lower=0,
                upper=30,
            ),
            version=str(data.get("version") or STAGE_D_VERSION),
        )


@dataclass
class StageDResult:
    run_id: int
    candidates: int = 0
    withheld_needs_review: int = 0
    selected: int = 0
    unselected: int = 0
    provider_attempts: int = 0
    reused: bool = False
    ai_failed: int = 0
    failed_phase: str | None = None
    errors: list[str] = field(default_factory=list)


def load_stage_d_profile(path: str | Path | None = None) -> StageDProfile:
    profile_path = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[1] / "config" / "daily_profile.yaml"
    )
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
) -> StageDResult:
    """Select Stage-C candidates and persist only the generic run-task result."""

    policy = _coerce_profile(profile if profile is not None else profile_path)
    result = StageDResult(run_id=int(run_id))
    owner = f"stage-d-selection:{int(run_id)}:{uuid4().hex}"

    try:
        with session_factory() as session:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id))
            if run is None or run.edition_id is None:
                raise StageDExecutionError(
                    "precondition",
                    "Stage D requires the current daily edition build",
                )

            candidate_event_ids = _load_stage_c_candidate_event_ids(repo, int(run_id))
            events = _load_candidate_events(
                session,
                run_id=int(run_id),
                event_ids=candidate_event_ids,
            )
            withheld_needs_review_ids = [
                int(event.id)
                for event in events
                if str(event.review_state or "").casefold() == "needs_review"
            ]
            withheld_needs_review_id_set = set(withheld_needs_review_ids)
            selectable_events = [
                event
                for event in events
                if int(event.id) not in withheld_needs_review_id_set
            ]
            selectable_event_ids = [int(event.id) for event in selectable_events]
            event_payload = [_selection_event(event) for event in selectable_events]
            result.candidates = len(event_payload)
            result.withheld_needs_review = len(withheld_needs_review_ids)
            edition = {
                "edition_date": run.edition_date,
                "candidate_count": len(event_payload),
                "withheld_needs_review_count": result.withheld_needs_review,
            }
            provider_payload = build_stage_d_provider_payload(
                event_payload,
                edition=edition,
                model=getattr(ai_client, "model", None),
                max_selected=policy.max_selected,
            )
            input_fingerprint = _response_hash(
                {
                    "provider_payload": provider_payload,
                    "withheld_needs_review_event_ids": withheld_needs_review_ids,
                }
            )
            config_fingerprint = _stage_d_config_fingerprint(policy, ai_client)
            stage = repo.ensure_stage(
                int(run_id),
                STAGE_D_NAME,
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
                metadata=_stage_d_stage_metadata(policy, ai_client),
                force=bool(force),
            )
            task = repo.ensure_stage_task(
                stage,
                subject_type="run",
                subject_id=int(run_id),
                target_run_id=int(run_id),
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
                metadata={
                    "phase": "selection",
                    "candidate_event_ids": selectable_event_ids,
                    "withheld_needs_review_event_ids": withheld_needs_review_ids,
                    "all_stage_c_candidate_event_ids": candidate_event_ids,
                },
                force=bool(force),
            )

            if not force:
                stored = _stored_selection(
                    task,
                    candidate_event_ids=selectable_event_ids,
                    max_selected=policy.max_selected,
                    input_fingerprint=input_fingerprint,
                    config_fingerprint=config_fingerprint,
                )
                if stored is not None:
                    result.selected = len(stored.selected)
                    result.unselected = result.candidates - result.selected
                    result.withheld_needs_review = len(withheld_needs_review_ids)
                    result.provider_attempts = int(
                        (_mapping(task.result)).get("provider_attempts") or 0
                    )
                    result.reused = True
                    return result

            claimed = repo.claim_stage_task(
                stage,
                task_id=task.id,
                owner=owner,
                force=bool(force),
                lease_seconds=_selection_lease_seconds(ai_client),
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
                acquire_stage=True,
            )
            if claimed is None:
                raise StageDExecutionError("selection", "run task is already running")
            task = claimed
            session.commit()

            raw_response: Any | None = None
            request_metadata: dict[str, Any] = {}
            try:
                if event_payload and policy.max_selected > 0:
                    parsed, attempts, raw_response, request_metadata = _call_selection_provider(
                        ai_client,
                        event_payload,
                        edition=edition,
                        max_selected=policy.max_selected,
                    )
                else:
                    parsed = StageDSelectionResponse.model_validate(
                        {
                            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                            "selected": [],
                        }
                    )
                    attempts = 0
                selected_rows = [row.model_dump(mode="json") for row in parsed.selected]
                completed = repo.complete_stage_task(
                    task,
                    owner=owner,
                    result_ref={
                        "phase": "selection",
                        "selected_event_ids": [row["event_id"] for row in selected_rows],
                    },
                    result={
                        "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                        "candidate_event_ids": selectable_event_ids,
                        "withheld_needs_review_event_ids": withheld_needs_review_ids,
                        "all_stage_c_candidate_event_ids": candidate_event_ids,
                        "selected": selected_rows,
                        "input_fingerprint": input_fingerprint,
                        "config_fingerprint": config_fingerprint,
                        "provider_attempts": attempts,
                    },
                    raw_response=raw_response,
                    metadata=request_metadata,
                )
                if completed is None:
                    raise StageDExecutionError(
                        "persistence",
                        "selection task lease was lost before completion",
                    )
                result.selected = len(selected_rows)
                result.unselected = result.candidates - result.selected
                result.provider_attempts = attempts
                run.selected = result.selected
                finished = repo.finish_stage(
                    stage,
                    status="succeeded",
                    owner=owner,
                    result_ref={
                        "selected_event_ids": [row["event_id"] for row in selected_rows],
                    },
                    metadata=_stage_d_stage_metadata(
                        policy,
                        ai_client,
                        candidate_count=result.candidates,
                        withheld_needs_review_count=result.withheld_needs_review,
                        selected_count=result.selected,
                        unselected_count=result.unselected,
                        provider_attempts=result.provider_attempts,
                    ),
                )
                if finished is None:
                    raise StageDExecutionError(
                        "persistence",
                        "selection stage lease was lost before completion",
                    )
                session.commit()
                return result
            except StageDExecutionError:
                session.rollback()
                raise
            except Exception as exc:
                result.ai_failed += 1
                result.failed_phase = "selection"
                result.errors.append(str(exc))
                failed = repo.fail_stage_task(
                    task,
                    owner=owner,
                    error_category="provider",
                    error_code=getattr(exc, "error_code", None) or "stage_d_selection_failed",
                    error_message=str(exc),
                    retryable=_provider_error_is_retryable(exc),
                    raw_response=getattr(exc, "raw_response", None),
                )
                if failed is None:
                    session.rollback()
                else:
                    session.commit()
                raise StageDExecutionError("selection", str(exc), cause=exc) from exc
    except StageDExecutionError:
        raise
    except Exception as exc:
        result.failed_phase = result.failed_phase or "persistence"
        result.errors.append(str(exc))
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
) -> StageDResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return run_stage_d_job(
        session_factory=create_session_factory(engine),
        profile=profile if profile is not None else profile_path,
        ai_client=ai_client if ai_client is not None else StageDSelectionClient.from_settings(settings),
        force=force,
        run_id=run_id,
    )


def _coerce_profile(
    value: StageDProfile | Mapping[str, Any] | str | Path | None,
) -> StageDProfile:
    if isinstance(value, StageDProfile):
        return value
    if isinstance(value, (str, Path)):
        return load_stage_d_profile(value)
    if isinstance(value, Mapping):
        return StageDProfile.from_mapping(value)
    return load_stage_d_profile()


def _load_stage_c_candidate_event_ids(repo: IntelRepository, run_id: int) -> list[int]:
    stage = repo.get_stage(int(run_id), "cluster")
    if stage is None or str(stage.status) != "succeeded":
        raise StageDExecutionError(
            "precondition",
            "Stage C cluster stage must succeed before Stage D",
        )
    task = repo.get_task(stage, subject_type="run", subject_id=int(run_id))
    if task is None or str(task.status) != "succeeded" or not isinstance(task.result, Mapping):
        raise StageDExecutionError(
            "precondition",
            "Stage C run task must succeed before Stage D",
        )
    if "candidate_event_ids" not in task.result:
        raise StageDExecutionError(
            "precondition",
            "Stage C result is missing candidate_event_ids; rerun Stage C",
        )
    return _strict_event_ids(task.result.get("candidate_event_ids"))


def _load_candidate_events(
    session: Session,
    *,
    run_id: int,
    event_ids: Sequence[int],
) -> list[IntelEvent]:
    if not event_ids:
        return []
    rows = list(
        session.scalars(
            select(IntelEvent).where(
                IntelEvent.build_id == int(run_id),
                IntelEvent.id.in_(list(event_ids)),
                IntelEvent.state == "candidate",
            )
        ).all()
    )
    by_id = {int(event.id): event for event in rows}
    missing = [event_id for event_id in event_ids if event_id not in by_id]
    if missing:
        raise StageDExecutionError(
            "precondition",
            f"Stage C candidate events are missing from the current build: {missing}",
        )
    return [by_id[event_id] for event_id in event_ids]


def _selection_event(event: IntelEvent) -> dict[str, Any]:
    source_groups = _json_strings(event.source_groups_json)
    if not source_groups and event.source_group:
        source_groups = [event.source_group]
    evidence = list(getattr(event, "evidence", ()) or ())
    verified = [row for row in evidence if str(row.status or "").casefold() == "verified"]
    return {
        "event_id": int(event.id),
        "title": str(event.title or ""),
        "summary_cn": str(event.summary_cn or ""),
        "topic": event.topic,
        "content_class": event.content_class,
        "keywords": _json_strings(event.keywords_json),
        "entities": _json_value(event.entities_json, []),
        "published_at": _iso_datetime(event.last_seen_at or event.first_seen_at),
        "display_score": float(event.display_score or 0.0),
        "source_groups": source_groups,
        "novelty_status": event.novelty_status,
        "risk_flags": _json_strings(event.risk_flags_json),
        "resolution_confidence": int(event.resolution_confidence or 0),
        "review_state": event.review_state,
        "verification_count": len(evidence),
        "verification_status": "verified" if verified else ("unverified" if evidence else "not_checked"),
    }


def _call_selection_provider(
    ai_client: Any | None,
    events: Sequence[Mapping[str, Any]],
    *,
    edition: Mapping[str, Any],
    max_selected: int,
) -> tuple[StageDSelectionResponse, int, Any | None, dict[str, Any]]:
    if ai_client is None or not callable(getattr(ai_client, "select", None)):
        raise RuntimeError("Stage D selection client is not configured")
    value = ai_client.select(
        events,
        edition=edition,
        max_selected=max_selected,
    )
    candidate_event_ids = [int(event["event_id"]) for event in events]
    if isinstance(value, StageDSelectionCallResult):
        raw_response = value.raw_response
        request_metadata = dict(value.request_metadata or {})
    else:
        raw_response = value
        request_metadata = {}
    try:
        parsed = strict_parse_stage_d_selection(
            value.parsed.model_dump(mode="json")
            if isinstance(value, StageDSelectionCallResult)
            else value,
            candidate_event_ids=candidate_event_ids,
            max_selected=max_selected,
        )
    except (TypeError, ValueError) as exc:
        raise StageDSelectionProviderError(
            f"Stage D response failed schema validation: {exc}",
            error_code="schema_validation_failed",
            raw_response=raw_response,
            request_metadata=request_metadata,
            cause=exc,
        ) from exc
    attempts = int(request_metadata.get("provider_attempts") or 1)
    return parsed, max(1, attempts), raw_response, request_metadata


def _stored_selection(
    task: IntelRunStageTask,
    *,
    candidate_event_ids: Sequence[int],
    max_selected: int,
    input_fingerprint: str,
    config_fingerprint: str,
) -> StageDSelectionResponse | None:
    if (
        task.status != "succeeded"
        or task.input_fingerprint != input_fingerprint
        or task.config_fingerprint != config_fingerprint
    ):
        return None
    stored = _mapping(task.result)
    if stored.get("schema_version") != STAGE_D_SELECTION_SCHEMA_VERSION:
        return None
    if _strict_event_ids(stored.get("candidate_event_ids")) != list(candidate_event_ids):
        return None
    return strict_parse_stage_d_selection(
        {
            "schema_version": stored.get("schema_version"),
            "selected": stored.get("selected"),
        },
        candidate_event_ids=candidate_event_ids,
        max_selected=max_selected,
    )


def _stage_d_stage_metadata(
    policy: StageDProfile,
    ai_client: Any | None,
    **counts: Any,
) -> dict[str, Any]:
    metadata = {
        "profile_version": policy.version,
        "stage_d_version": STAGE_D_VERSION,
        "prompt_version": STAGE_D_SELECTION_PROMPT_VERSION,
        "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
        "max_selected": policy.max_selected,
        "model": getattr(ai_client, "model", None),
    }
    metadata.update({key: value for key, value in counts.items() if value is not None})
    return metadata


def _stage_d_config_fingerprint(policy: StageDProfile, ai_client: Any | None) -> str:
    return _response_hash(
        {
            "stage_d_version": STAGE_D_VERSION,
            "profile_version": policy.version,
            "prompt_version": STAGE_D_SELECTION_PROMPT_VERSION,
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "model": getattr(ai_client, "model", None),
            "transport": "responses",
            "max_selected": policy.max_selected,
        }
    )


def _selection_lease_seconds(ai_client: Any | None) -> int:
    try:
        timeout = max(1.0, float(getattr(ai_client, "timeout_seconds", 120.0)))
    except (TypeError, ValueError, OverflowError):
        timeout = 120.0
    try:
        attempts = max(1, int(getattr(ai_client, "max_retries", 0)) + 1)
    except (TypeError, ValueError, OverflowError):
        attempts = 1
    return max(600, int(math.ceil(timeout * attempts + 120.0)))


def _provider_error_is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, StageDSelectionProviderError):
        return exc.retryable
    status_code = getattr(exc, "status_code", None)
    try:
        if status_code is not None:
            return int(status_code) == 429 or int(status_code) >= 500
    except (TypeError, ValueError, OverflowError):
        pass
    name = exc.__class__.__name__.casefold()
    text = str(exc).casefold()
    return any(token in name or token in text for token in ("timeout", "connect", "network", "transport"))


def _strict_event_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise StageDExecutionError("precondition", "candidate_event_ids must be a JSON array")
    result: list[int] = []
    for raw in value:
        if isinstance(raw, bool):
            raise StageDExecutionError("precondition", "candidate_event_ids contains a non-integer ID")
        try:
            event_id = int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise StageDExecutionError(
                "precondition",
                "candidate_event_ids contains a non-integer ID",
                cause=exc,
            ) from exc
        if event_id <= 0 or event_id in result:
            raise StageDExecutionError(
                "precondition",
                "candidate_event_ids must contain unique positive IDs",
            )
        result.append(event_id)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


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
    if not isinstance(raw, list):
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
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _bounded_int(value: Any, default: int, *, lower: int, upper: int) -> int:
    try:
        return max(lower, min(upper, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


__all__ = [
    "STAGE_D_NAME",
    "STAGE_D_VERSION",
    "StageDExecutionError",
    "StageDProfile",
    "StageDResult",
    "load_stage_d_profile",
    "run_stage_d_from_settings",
    "run_stage_d_job",
]
