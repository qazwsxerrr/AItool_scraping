"""Stage D: one AI editorial selection over the bounded Stage-C event pool."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.skills.stage_d_editorial import (
    StageDEditorialClient,
    StageDEditorialResponse,
    StageDProviderCallResult,
    strict_parse_stage_d_editorial,
)
from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.config.settings import Settings
from app.domain.policies import is_first_party_x_source
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
STAGE_D_VERSION = "stage-d-v4"
STAGE_D_PROMPT_VERSION = "stage_d_editorial_v4"
DEFAULT_STAGE_D_WATCHLIST_MAX = 10


class StageDExecutionError(RuntimeError):
    """Terminal Stage-D execution failure that blocks downstream export."""

    def __init__(self, phase: str, message: str, *, cause: BaseException | None = None) -> None:
        self.phase = str(phase)
        self.cause = cause
        super().__init__(f"stage_d {self.phase} failed: {message}")


@dataclass(frozen=True)
class StageDProfile:
    """Durable policy for the single Stage-D editorial call."""

    total_max: int = DEFAULT_DAILY_REPORT_LIMIT
    watchlist_max: int = DEFAULT_STAGE_D_WATCHLIST_MAX
    recent_history_days: int = 3
    version: str = STAGE_D_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "StageDProfile":
        data = dict(value or {})
        return cls(
            total_max=_bounded_int(data.get("total_max"), DEFAULT_DAILY_REPORT_LIMIT, lower=0, upper=30),
            watchlist_max=_bounded_int(
                data.get("watchlist_max"), DEFAULT_STAGE_D_WATCHLIST_MAX, lower=0, upper=30
            ),
            recent_history_days=_bounded_int(data.get("recent_history_days"), 3, lower=0, upper=30),
            version=str(data.get("version") or STAGE_D_VERSION),
        )


@dataclass
class StageDResult:
    run_id: int
    processed: int = 0
    eligible: int = 0
    selected: int = 0
    omitted: int = 0
    watchlist: int = 0
    snapshots: int = 0
    provider_attempts: int = 0
    ai_failed: int = 0
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
    """Run one complete Stage-D editorial selection and persist its snapshot."""

    policy = _coerce_profile(profile if profile is not None else profile_path)
    result = StageDResult(run_id=run_id)
    owner = "stage-d-editorial"
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

            eligible = [
                candidate
                for candidate in candidates
                if not candidate.get("pre_editorial_reason")
            ]
            result.eligible = len(eligible)

            event_payload = [_prompt_event(candidate) for candidate in eligible]
            input_fingerprint = _stage_d_input_fingerprint(eligible, policy)
            config_fingerprint = _stage_d_config_fingerprint(policy, ai_client)
            task = repo.ensure_stage_task(
                stage,
                subject_type="run",
                subject_id=int(run_id),
                target_run_id=int(run_id),
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
                metadata={
                    "phase": "editorial",
                    "event_ids": [int(row["event_id"]) for row in event_payload],
                    "candidate_count": len(event_payload),
                },
            )

            decisions: dict[int, dict[str, Any]] = {}
            editorial_audit: dict[str, Any] = {"request_metadata": {}, "raw_response": None}
            if not force:
                stored = _stored_editorial(task, event_payload, input_fingerprint, config_fingerprint)
                if stored is not None:
                    decisions = stored
                    editorial_audit = dict(task.result or {}).get("audit") or editorial_audit

            if not decisions and event_payload:
                claimed = repo.claim_stage_task(
                    stage,
                    task_id=task.id,
                    owner=owner,
                    force=bool(force),
                    input_fingerprint=input_fingerprint,
                    config_fingerprint=config_fingerprint,
                    acquire_stage=True,
                )
                if claimed is None:
                    raise StageDExecutionError("editorial", "run task is already running")
                task = claimed
                session.commit()
                try:
                    parsed, attempts, editorial_audit = _call_editorial_provider(
                        ai_client,
                        event_payload,
                        edition={
                            "date": run.edition_date,
                            "max_selected": policy.total_max,
                            "max_watchlist": policy.watchlist_max,
                            "candidate_count": len(event_payload),
                        },
                        total_max=policy.total_max,
                        watchlist_max=policy.watchlist_max,
                    )
                    decisions = _decision_rows(parsed)
                    result.provider_attempts = attempts
                    repo.complete_stage_task(
                        task,
                        owner=owner,
                        result_ref={"phase": "editorial"},
                        result={
                            "phase": "editorial",
                            "event_ids": [int(row["event_id"]) for row in event_payload],
                            "input_fingerprint": input_fingerprint,
                            "config_fingerprint": config_fingerprint,
                            "decisions": list(decisions.values()),
                            "provider_attempts": attempts,
                            "audit": editorial_audit,
                        },
                        raw_response=editorial_audit.get("raw_response"),
                        metadata=editorial_audit,
                    )
                    session.commit()
                except Exception as exc:
                    result.ai_failed += 1
                    result.failed_phase = "editorial"
                    result.errors.append(str(exc))
                    repo.fail_stage_task(
                        task,
                        owner=owner,
                        error_category="provider",
                        error_code=getattr(exc, "error_code", None) or "editorial_failed",
                        error_message=str(exc),
                        retryable=False,
                        raw_response=getattr(exc, "raw_response", None),
                    )
                    session.commit()
                    raise StageDExecutionError("editorial", str(exc), cause=exc) from exc
            elif not event_payload:
                # Empty eligible pool is a valid, auditable daily outcome.
                repo.complete_stage_task(
                    task,
                    owner=owner,
                    result_ref={"phase": "editorial"},
                    result={
                        "phase": "editorial",
                        "event_ids": [],
                        "input_fingerprint": input_fingerprint,
                        "config_fingerprint": config_fingerprint,
                        "decisions": [],
                        "provider_attempts": 0,
                        "audit": editorial_audit,
                    },
                    metadata=editorial_audit,
                )
                session.commit()

            _replace_stage_d_snapshot(
                repo,
                run_id=run_id,
                candidates=candidates,
                decisions=decisions,
                policy=policy,
                result=result,
            )
            session.commit()
            stage_metadata = _stage_d_stage_metadata(
                policy,
                ai_client,
                candidate_count=len(eligible),
                selected_count=result.selected,
                watchlist_count=result.watchlist,
                omitted_count=result.omitted,
                provider_attempts=result.provider_attempts,
            )
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
        "prompt_version": STAGE_D_PROMPT_VERSION,
        "total_max": policy.total_max,
        "watchlist_max": policy.watchlist_max,
        "recent_history_days": policy.recent_history_days,
        "model": getattr(ai_client, "model", None),
    }
    metadata.update({key: int(value) for key, value in counts.items() if value is not None})
    return metadata


def _stage_d_config_fingerprint(policy: StageDProfile, ai_client: Any | None) -> str:
    return ":".join(
        (
            STAGE_D_VERSION,
            policy.version,
            STAGE_D_PROMPT_VERSION,
            str(getattr(ai_client, "model", None) or "unconfigured"),
        )
    )


def _call_editorial_provider(
    ai_client: Any | None,
    events: Sequence[Mapping[str, Any]],
    *,
    edition: Mapping[str, Any],
    total_max: int,
    watchlist_max: int,
) -> tuple[StageDEditorialResponse, int, dict[str, Any]]:
    if ai_client is None or not callable(getattr(ai_client, "select_events", None)):
        raise RuntimeError("Stage D editorial client is not configured")
    value = ai_client.select_events(
        events,
        edition=edition,
        total_max=total_max,
        watchlist_max=watchlist_max,
    )
    parsed, audit = _provider_envelope(value)
    if isinstance(value, StageDProviderCallResult):
        # The concrete client has already performed schema and local-guard
        # validation before returning its audit envelope.
        if not isinstance(parsed, StageDEditorialResponse):
            raise TypeError("Stage D provider returned an invalid parsed response")
    else:
        raw = parsed.model_dump(mode="json") if isinstance(parsed, StageDEditorialResponse) else parsed
        parsed = strict_parse_stage_d_editorial(
            raw,
            event_ids=[int(event["event_id"]) for event in events],
            total_max=total_max,
            watchlist_max=watchlist_max,
            events=events,
        )
    attempts = int((audit.get("request_metadata") or {}).get("provider_attempts") or 1)
    return parsed, max(1, attempts), audit


def _provider_envelope(value: Any) -> tuple[Any, dict[str, Any]]:
    if isinstance(value, StageDProviderCallResult):
        return value.parsed, {
            "raw_response": value.raw_response,
            "request_metadata": dict(value.request_metadata or {}),
        }
    return value, {"raw_response": None, "request_metadata": {}}


def _replace_stage_d_snapshot(
    repo: IntelRepository,
    *,
    run_id: int,
    candidates: Sequence[Mapping[str, Any]],
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
        if candidate.get("pre_editorial_reason"):
            reason = str(candidate.get("pre_editorial_reason"))
            reason_code = "recent_repeat_without_material_update" if reason.startswith("repeat") else "low_signal"
            decision = _omitted_decision(reason_code, "本地规则：" + ("近期重复且没有材料更新。" if reason.startswith("repeat") else "事件分数低于 60，省略。"), event_id=event_id)
            tier = "omitted"
        else:
            decision = dict(decisions.get(event_id) or _omitted_decision(
                "provider_missing_decision",
                "未获得可展示的编辑决策。",
                event_id=event_id,
            ))
            tier = str(decision.get("decision") or "omitted")

        if tier == "selected":
            selected_order += 1
            display_order = max(1, int(decision.get("display_order") or selected_order))
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
            "editorial_tier": tier,
            "decision": decision.get("decision"),
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
            "novelty_status": candidate.get("novelty_status"),
            "prior_event_key": candidate.get("prior_event_key"),
            "delta_summary": candidate.get("delta_summary"),
            "changed_facts": candidate.get("changed_facts", []),
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
            reason=(decision.get("reason_codes") or ["omitted"])[0],
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
    stmt = stmt.where(IntelEvent.build_id == int(run_id), IntelEvent.id.in_(ids or [-1]))
    return list(session.scalars(stmt).unique().all())


def _load_current_cluster_event_ids(session: Session, run_id: int) -> list[int]:
    stage = session.scalar(
        select(IntelRunStage).where(IntelRunStage.run_id == run_id, IntelRunStage.stage_name == "cluster")
    )
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
    source_evidence_level = (
        "multi_community_signal" if community_group_count >= 2 else "single_community_signal"
    ) if community_only else "trusted_or_first_party_supported"
    metadata = _json_value(event.resolution_raw_json, {})
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    pre_editorial_reason = None
    if str(event.novelty_status or "").casefold() == "repeat":
        pre_editorial_reason = "repeat_without_material_update"
    elif _number(event.display_score) < 60:
        pre_editorial_reason = "low_score"
    return {
        "event": event,
        "topic": str(event.topic or "technology_insight").strip().casefold() or "technology_insight",
        "content_class": str(event.content_class or "").strip() or None,
        "source_group": event.source_group or (source_groups[0] if source_groups else None),
        "source_groups": tuple(dict.fromkeys(source_groups)),
        "source_ids": tuple(dict.fromkeys(source_ids)),
        "community_source_group_count": community_group_count,
        "source_evidence_level": source_evidence_level,
        "novelty_status": str(event.novelty_status or "unknown"),
        "event_score_components": metadata.get("score_components", {}),
        "event_score_band": metadata.get("score_band") or ("low" if _number(event.display_score) < 60 else ("medium" if _number(event.display_score) < 75 else "high")),
        "prior_event_key": metadata.get("prior_event_key"),
        "delta_summary": metadata.get("delta_summary"),
        "changed_facts": metadata.get("changed_facts", []),
        "pre_editorial_reason": pre_editorial_reason,
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
        "resolution_method": event.resolution_method,
        "resolution_confidence": int(event.resolution_confidence or 0),
        "novelty_status": candidate.get("novelty_status"),
        "event_score_band": candidate.get("event_score_band"),
        "event_score_components": candidate.get("event_score_components"),
        "prior_event_key": candidate.get("prior_event_key"),
        "delta_summary": candidate.get("delta_summary"),
        "changed_facts": candidate.get("changed_facts"),
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


def _stored_editorial(
    task: IntelRunStageTask,
    events: Sequence[Mapping[str, Any]],
    input_fingerprint: str,
    config_fingerprint: str,
) -> dict[int, dict[str, Any]] | None:
    if not task or not _task_is_reusable(task, input_fingerprint, config_fingerprint):
        return None
    stored = task.result
    if not isinstance(stored, Mapping) or stored.get("phase") != "editorial":
        return None
    rows = _decision_rows(stored)
    expected = {int(event["event_id"]) for event in events}
    return rows if set(rows) == expected else None


def _decision_rows(value: Any) -> dict[int, dict[str, Any]]:
    decisions = getattr(value, "decisions", None)
    if decisions is None and isinstance(value, Mapping):
        decisions = value.get("decisions")
    result: dict[int, dict[str, Any]] = {}
    for decision in decisions or []:
        row = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else dict(decision)
        result[int(row["event_id"])] = dict(row)
    return result


def _task_is_reusable(task: IntelRunStageTask, input_fingerprint: str, config_fingerprint: str) -> bool:
    return (
        task.status == "succeeded"
        and task.input_fingerprint == str(input_fingerprint)
        and task.config_fingerprint == str(config_fingerprint)
    )


def _omitted_decision(reason_code: str, reason: str, *, event_id: int | None = None) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "decision": "omitted",
        "editorial_score": 0,
        "story_family_id": f"omitted_{event_id or 'unknown'}",
        "family_position": None,
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
        "profile": {
            "version": policy.version,
            "total_max": policy.total_max,
            "watchlist_max": policy.watchlist_max,
            "recent_history_days": policy.recent_history_days,
        },
        "events": [
            {
                "id": int(candidate["event"].id),
                "title": candidate["event"].title,
                "summary_cn": candidate["event"].summary_cn,
                "display_score": candidate["event"].display_score,
                "topic": candidate["event"].topic,
                "keywords": candidate["event"].keywords_json,
                "entities": candidate["event"].entities_json,
                "last_seen_at": _iso_datetime(candidate["event"].last_seen_at),
                "recent_daily_history": candidate.get("recent_daily_history"),
            }
            for candidate in candidates
        ],
    }
    return _response_hash(payload)


def _relation_is_community(relation: IntelEventItem) -> bool:
    item = relation.item
    source = relation.source or (item.source if item is not None else None)
    if source is not None and is_first_party_x_source(source):
        return False
    content_class = str(
        (item.ai_review.content_class if item is not None and item.ai_review is not None else None)
        or (item.content_class if item is not None else None)
        or ""
    ).strip()
    return content_class == "community_social"


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


__all__ = [
    "STAGE_D_NAME",
    "StageDExecutionError",
    "StageDProfile",
    "StageDResult",
    "load_stage_d_profile",
    "run_stage_d_from_settings",
    "run_stage_d_job",
]
