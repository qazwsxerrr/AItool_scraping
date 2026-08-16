"""Event-level editorial ranking and deterministic quota enforcement.

Wave 3 deliberately ranks :class:`~app.storage.models.IntelEvent` rows using
their already aggregated ``display_score``.  Raw feed ``selection_score``
values are never compared here: that score belongs to the item/source
selection stage.  An optional AI adapter may provide an ordering, but all
hard gates and quotas are applied locally and a provider failure falls back to
the deterministic display-score ordering.
"""

from __future__ import annotations

import json
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventItem, IntelItem, IntelRun, IntelRunStage, IntelRunStageTask
from app.storage.repository import IntelRepository


LOGGER = logging.getLogger(__name__)

DEFAULT_TOPIC_CAPS: dict[str, int] = {
    "model": 16,
    "product": 12,
    "project": 12,
    "industry": 8,
    "tutorial": 5,
    "opinion": 4,
    "paper": 3,
}


@dataclass(frozen=True)
class EditorialProfile:
    """Validated daily ranking policy.

    The loader accepts a few historical spellings (``topic_maxima`` and
    ``content_class_caps`` for example) so callers can pass a small test
    mapping without coupling to one YAML key name.  Empty source maps mean no
    additional cap; ``*`` acts as a wildcard cap.
    """

    snapshot_key: str = "latest"
    total_max: int = DEFAULT_DAILY_REPORT_LIMIT
    topic_caps: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TOPIC_CAPS))
    content_class_maxima: dict[str, int] = field(default_factory=dict)
    source_group_maxima: dict[str, int] = field(default_factory=dict)
    source_id_maxima: dict[str, int] = field(default_factory=dict)
    preferred_minima: dict[str, Any] = field(default_factory=dict)
    paper_hard_gate: bool = True
    version: str = "wave3-v1"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "EditorialProfile":
        data = dict(value or {})
        quotas = data.get("quotas") if isinstance(data.get("quotas"), Mapping) else {}
        topic_caps = _int_map(
            _first_mapping(data, quotas, "topic_caps", "topic_maxima", "topics", "max_per_topic")
        )
        if not topic_caps:
            topic_caps = dict(DEFAULT_TOPIC_CAPS)
        else:
            # A partial profile should not silently remove one of the seven
            # hard topic caps.
            topic_caps = {**DEFAULT_TOPIC_CAPS, **topic_caps}
            topic_caps = {
                key: min(DEFAULT_TOPIC_CAPS.get(key, value), value)
                for key, value in topic_caps.items()
            }
        content_caps = _int_map(
            _first_mapping(
                data,
                quotas,
                "content_class_maxima",
                "content_class_caps",
                "content_classes",
            )
        )
        source_group_caps = _int_map(
            _first_mapping(data, quotas, "source_group_maxima", "source_group_caps", "source_groups")
        )
        source_id_caps = _int_map(
            _first_mapping(data, quotas, "source_id_maxima", "source_id_caps", "source_ids")
        )
        preferred = data.get("preferred_minima", data.get("preferred", {}))
        if not isinstance(preferred, Mapping):
            preferred = {}
        total_value = data.get(
            "total_max",
            data.get("max_total", quotas.get("total_max", DEFAULT_DAILY_REPORT_LIMIT)),
        )
        try:
            total_max = max(0, int(total_value))
        except (TypeError, ValueError, OverflowError):
            total_max = DEFAULT_DAILY_REPORT_LIMIT
        paper = data.get("paper") if isinstance(data.get("paper"), Mapping) else {}
        paper_gate = _coerce_bool(data.get("paper_hard_gate", paper.get("hard_gate", True)), True)
        return cls(
            snapshot_key=str(data.get("snapshot_key") or "latest"),
            total_max=total_max,
            topic_caps=topic_caps,
            content_class_maxima=content_caps,
            source_group_maxima=source_group_caps,
            source_id_maxima=source_id_caps,
            preferred_minima=dict(preferred),
            paper_hard_gate=bool(paper_gate),
            version=str(data.get("version") or "wave3-v1"),
        )


@dataclass
class EditorialRankResult:
    run_id: int | None = None
    snapshot_key: str = "latest"
    processed: int = 0
    selected: int = 0
    rejected: int = 0
    snapshots: int = 0
    ai_ranked: int = 0
    ai_failed: int = 0
    used_fallback: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ranked(self) -> int:
        return self.processed


def load_daily_profile(path: str | Path | None = None) -> EditorialProfile:
    """Load the checked-in daily profile, returning safe defaults if absent."""

    profile_path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / "config" / "daily_profile.yaml"
    if not profile_path.exists():
        return EditorialProfile.from_mapping(None)
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        LOGGER.warning("Unable to read editorial profile %s: %s", profile_path, exc)
        return EditorialProfile.from_mapping(None)
    return EditorialProfile.from_mapping(raw if isinstance(raw, Mapping) else None)


def run_editorial_rank_job(
    *,
    session_factory: sessionmaker[Session],
    profile: EditorialProfile | Mapping[str, Any] | str | Path | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    limit: int | None = None,
    force: bool = False,
    snapshot_key: str | None = None,
    run_id: int | None = None,
    event_ids: Iterable[int] | None = None,
) -> EditorialRankResult:
    """Rank candidate events and write one idempotent ranking snapshot."""

    policy = _coerce_profile(profile if profile is not None else profile_path)
    key = str(snapshot_key or (f"run-{int(run_id)}" if run_id is not None else policy.snapshot_key) or "latest")
    result = EditorialRankResult(run_id=run_id, snapshot_key=key)
    stage = None
    stage_task = None
    owner = "editorial-rank"
    with session_factory() as session:
        try:
            repo = IntelRepository(session)
            run = session.get(IntelRun, int(run_id)) if run_id is not None else None
            if run is not None:
                stage = repo.ensure_stage(
                    int(run_id),
                    "rank",
                    metadata={"snapshot_key": key, "profile_version": policy.version},
                )
            if run_id is not None and event_ids is None:
                # A run-scoped rank is downstream of Stage C.  Read the
                # latest successful cluster task projection instead of
                # querying every event ever marked ``new_in_run_id``.  That
                # column is historical provenance and is intentionally not a
                # current-run membership index.
                event_ids = _load_current_cluster_event_ids(session, int(run_id))
            events = _load_events(session, limit=limit, run_id=run_id, event_ids=event_ids)
            result.processed = len(events)
            if stage is not None:
                input_fingerprint = _rank_input_fingerprint(events, policy, key)
                stage_task = repo.ensure_stage_task(
                    stage,
                    subject_type="run",
                    subject_id=int(run_id),
                    target_run_id=int(run_id),
                    input_fingerprint=input_fingerprint,
                    config_fingerprint=f"rank-v1:{policy.version}",
                )
                claimed = repo.claim_stage_task(
                    stage,
                    task_id=stage_task.id,
                    owner=owner,
                    # Ranking is a cheap deterministic projection.  Rebuild
                    # the run snapshot on each explicit invocation while
                    # retaining the durable attempt history.
                    force=True if force or stage_task.status == "succeeded" else False,
                    input_fingerprint=input_fingerprint,
                    config_fingerprint=f"rank-v1:{policy.version}",
                )
                if claimed is None:
                    result.errors.append("editorial rank stage is already running")
                    result.ai_failed = 1
                    return result
                stage_task = claimed
                # Persist the lease before any optional provider call so a
                # concurrent rank command cannot start the same run.
                session.commit()
            candidates = [_candidate(event, run_id=run_id) for event in events]
            ai_order: dict[int, tuple[int, float | None]] = {}
            rank_source = "deterministic"
            if ai_client is not None and candidates:
                try:
                    ai_order = _ai_rank(ai_client, candidates)
                    if ai_order:
                        result.ai_ranked = len(ai_order)
                        rank_source = "ai"
                except Exception as exc:
                    result.ai_failed = 1
                    result.used_fallback = True
                    result.errors.append(str(exc))
                    rank_source = "deterministic_fallback"
                    LOGGER.warning("Editorial rank AI failed; using deterministic fallback: %s", exc)
            ordered = _ordered_candidates(candidates, ai_order, policy)
            selected_ids, reasons, metadata = _select_with_quotas(ordered, policy)

            repo.clear_event_ranking_snapshot(snapshot_key=key)
            for rank, candidate in enumerate(ordered, start=1):
                event = candidate["event"]
                selected = event.id in selected_ids
                reason = reasons.get(event.id, "not_selected")
                if selected:
                    result.selected += 1
                else:
                    result.rejected += 1
                ai_meta = metadata.get(event.id, {})
                snapshot = repo.upsert_event_ranking_snapshot(
                    event.id,
                    snapshot_key=key,
                    run_id=run_id,
                    rank=rank,
                    # This is intentionally the event-level score.  Do not
                    # substitute the member item's raw selection_score.
                    display_score=float(event.display_score or 0.0),
                    selected=selected,
                    topic=candidate["topic"],
                    source_group=candidate["source_group"],
                    content_class=candidate["content_class"],
                    reason=reason,
                    metadata={
                        "profile_version": policy.version,
                        "rank_source": rank_source,
                        "paper_gate_pass": candidate["paper_gate_pass"],
                        **ai_meta,
                    },
                )
                result.snapshots += int(snapshot.created)
            session.commit()
            if stage_task is not None:
                repo.complete_stage_task(
                    stage_task,
                    owner=owner,
                    result={"selected": result.selected, "snapshot_key": key},
                )
                session.commit()
        except Exception as exc:
            session.rollback()
            result.errors.append(str(exc))
            LOGGER.exception("Editorial ranking failed")
            if stage is not None:
                try:
                    with session.begin():
                        state_repo = IntelRepository(session)
                        state_stage = state_repo.get_stage(int(run_id), "rank")
                        state_task = state_repo.get_task(state_stage, subject_type="run", subject_id=int(run_id)) if state_stage else None
                        if state_task is not None and state_task.status == "running":
                            state_repo.fail_stage_task(
                                state_task,
                                error_category="stage",
                                error_code="rank_failed",
                                error_message=str(exc),
                                retryable=True,
                                owner=owner,
                            )
                except Exception:
                    LOGGER.exception("Unable to persist editorial rank failure")
    return result


def run_editorial_rank_from_settings(
    *,
    settings: Settings,
    profile: EditorialProfile | Mapping[str, Any] | str | Path | None = None,
    profile_path: str | Path | None = None,
    ai_client: Any | None = None,
    limit: int | None = None,
    force: bool = False,
    snapshot_key: str | None = None,
    run_id: int | None = None,
    event_ids: Iterable[int] | None = None,
) -> EditorialRankResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    selected_profile = profile
    if selected_profile is None:
        selected_profile = profile_path
    if selected_profile is None:
        selected_profile = load_daily_profile()
    return run_editorial_rank_job(
        session_factory=create_session_factory(engine),
        profile=selected_profile,
        ai_client=ai_client,
        limit=limit,
        force=force,
        snapshot_key=snapshot_key,
        run_id=run_id,
        event_ids=event_ids,
    )


def _coerce_profile(profile: EditorialProfile | Mapping[str, Any] | str | Path | None) -> EditorialProfile:
    if isinstance(profile, EditorialProfile):
        return profile
    if isinstance(profile, (str, Path)):
        return load_daily_profile(profile)
    if isinstance(profile, Mapping):
        return EditorialProfile.from_mapping(profile)
    return load_daily_profile()


def _load_events(
    session: Session,
    *,
    limit: int | None,
    run_id: int | None,
    event_ids: Iterable[int] | None,
) -> list[IntelEvent]:
    stmt = (
        select(IntelEvent)
        .options(
            joinedload(IntelEvent.event_items)
            .joinedload(IntelEventItem.item)
            .joinedload(IntelItem.ai_review),
            joinedload(IntelEvent.event_items)
            .joinedload(IntelEventItem.item)
            .joinedload(IntelItem.source),
            joinedload(IntelEvent.event_items).joinedload(IntelEventItem.source),
        )
        .where(IntelEvent.state.not_in(("rejected", "discarded", "filtered")))
        .order_by(IntelEvent.display_score.desc(), IntelEvent.event_key.asc(), IntelEvent.id.asc())
    )
    ids = [int(value) for value in event_ids] if event_ids is not None else []
    if event_ids is not None:
        stmt = stmt.where(IntelEvent.id.in_(ids or [-1]))
    elif run_id is not None:
        # A run-scoped rank must receive the Stage-C current projection
        # explicitly.  ``new_in_run_id`` is historical provenance and can
        # contain stale rows after a same-run force rerun, so never use it as
        # an implicit current-membership query.
        stmt = stmt.where(IntelEvent.id.in_([-1]))
    else:
        latest_run = session.scalar(select(func.max(IntelRun.id)))
        stmt = stmt.where(IntelEvent.new_in_run_id == latest_run if latest_run else IntelEvent.new_in_run_id.is_(None))
    if limit is not None:
        stmt = stmt.limit(max(0, int(limit)))
    return list(session.scalars(stmt).unique().all())


def _load_current_cluster_event_ids(session: Session, run_id: int) -> list[int]:
    """Return the latest persisted Stage-C current projection for a run.

    ``current_event_ids`` was added to the task result without a schema
    migration.  Older task payloads only contain ``event_ids`` (new-event
    ids), which remains a safe compatibility fallback; a missing task or an
    empty payload deliberately resolves to an empty projection.
    """

    stage = session.scalar(
        select(IntelRunStage).where(
            IntelRunStage.run_id == int(run_id),
            IntelRunStage.stage_name == "cluster",
        )
    )
    if stage is None:
        return []
    task = session.scalar(
        select(IntelRunStageTask).where(
            IntelRunStageTask.stage_id == stage.id,
            IntelRunStageTask.subject_type == "run",
            IntelRunStageTask.subject_id == str(int(run_id)),
        )
    )
    if task is None or task.status != "succeeded":
        return []
    payload = task.result
    if not isinstance(payload, Mapping):
        return []
    value = payload.get("current_event_ids")
    if value is None:
        value = payload.get("event_ids")
    return _normalize_event_ids(value)


def _normalize_event_ids(value: Any) -> list[int]:
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


def _rank_input_fingerprint(
    events: Sequence[IntelEvent],
    policy: EditorialProfile,
    snapshot_key: str,
) -> str:
    payload = {
        "snapshot_key": snapshot_key,
        "profile_version": policy.version,
        "total_max": policy.total_max,
        "events": [
            {
                "id": int(event.id),
                "score": float(event.display_score or 0.0),
                "state": event.state,
            }
            for event in events
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _candidate(event: IntelEvent, *, run_id: int | None = None) -> dict[str, Any]:
    source_groups = _json_strings(event.source_groups_json)
    source_ids = _json_strings(event.source_ids_json)
    if not source_groups and event.source_group:
        source_groups = [event.source_group]
    if not source_ids:
        source_ids = [relation.source_id for relation in event.event_items if relation.source_id]
    content_class = str(event.content_class or "").strip() or None
    topic = str(event.topic or "opinion").strip().casefold() or "opinion"
    gate_pass, gate_reason = _paper_gate(event)
    if run_id is None:
        has_retained_member = any(
            relation.item is not None and relation.item.status == "candidate"
            for relation in event.event_items
        )
    else:
        # Run-scoped ranking uses event provenance, not the mutable global item
        # status projection.  A repeat event is retained when Stage C attached
        # a member with this run's lineage or the event itself was new in it.
        has_retained_member = (
            event.new_in_run_id == int(run_id)
            or event.last_run_id == int(run_id)
            or event.first_run_id == int(run_id)
        )
        if not has_retained_member:
            for relation in event.event_items:
                try:
                    lineage = json.loads(relation.lineage_json or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    lineage = {}
                if isinstance(lineage, Mapping) and lineage.get("run_id") == int(run_id):
                    has_retained_member = True
                    break
    return {
        "event": event,
        "topic": topic,
        "content_class": content_class,
        "source_group": event.source_group or (source_groups[0] if source_groups else None),
        "source_groups": tuple(dict.fromkeys(source_groups)),
        "source_ids": tuple(dict.fromkeys(source_ids)),
        "display_score": _number(event.display_score),
        "paper_gate_pass": gate_pass,
        "paper_gate_reason": gate_reason,
        "has_retained_member": has_retained_member,
    }


def _ordered_candidates(
    candidates: Sequence[dict[str, Any]],
    ai_order: Mapping[int, tuple[int, float | None]],
    profile: EditorialProfile,
) -> list[dict[str, Any]]:
    def key(candidate: dict[str, Any]) -> tuple[Any, ...]:
        event = candidate["event"]
        ai_rank, ai_score = ai_order.get(int(event.id), (10**9, None))
        # Preferred minima are soft: they only affect ordering while a quota
        # is still available and never cause synthetic padding.
        preference = _preference_bonus(candidate, profile)
        return (
            ai_rank,
            -(ai_score if ai_score is not None else candidate["display_score"]),
            -candidate["display_score"],
            -preference,
            str(event.event_key or ""),
            int(event.id),
        )

    return sorted(candidates, key=key)


def _select_with_quotas(
    ordered: Sequence[dict[str, Any]],
    profile: EditorialProfile,
) -> tuple[set[int], dict[int, str], dict[int, dict[str, Any]]]:
    selected: set[int] = set()
    reasons: dict[int, str] = {}
    metadata: dict[int, dict[str, Any]] = {}
    counts: dict[str, dict[str, int]] = {
        "topic": {},
        "content_class": {},
        "source_group": {},
        "source_id": {},
    }
    for candidate in ordered:
        event = candidate["event"]
        event_id = int(event.id)
        if not candidate["has_retained_member"]:
            reasons[event_id] = "candidate:not_retained"
            metadata[event_id] = {"quota_rejection": "not_retained"}
            continue
        if profile.paper_hard_gate and candidate["topic"] == "paper" and not candidate["paper_gate_pass"]:
            reasons[event_id] = candidate["paper_gate_reason"] or "paper_gate"
            metadata[event_id] = {"quota_rejection": candidate["paper_gate_reason"] or "paper_gate"}
            continue
        if len(selected) >= profile.total_max:
            reasons[event_id] = "quota:total_max"
            metadata[event_id] = {"quota_rejection": "total_max"}
            continue
        topic = candidate["topic"]
        topic_cap = _cap(profile.topic_caps, topic)
        if topic_cap is not None and counts["topic"].get(topic, 0) >= topic_cap:
            reasons[event_id] = f"quota:topic={topic}"
            metadata[event_id] = {"quota_rejection": "topic", "topic": topic}
            continue
        content_class = candidate["content_class"]
        content_cap = _cap(profile.content_class_maxima, content_class)
        if content_cap is not None and counts["content_class"].get(content_class or "", 0) >= content_cap:
            reasons[event_id] = f"quota:content_class={content_class}"
            metadata[event_id] = {"quota_rejection": "content_class", "content_class": content_class}
            continue
        blocked_group = _blocked_dimension(candidate["source_groups"], counts["source_group"], profile.source_group_maxima)
        if blocked_group:
            reasons[event_id] = f"quota:source_group={blocked_group}"
            metadata[event_id] = {"quota_rejection": "source_group", "source_group": blocked_group}
            continue
        blocked_source = _blocked_dimension(candidate["source_ids"], counts["source_id"], profile.source_id_maxima)
        if blocked_source:
            reasons[event_id] = f"quota:source_id={blocked_source}"
            metadata[event_id] = {"quota_rejection": "source_id", "source_id": blocked_source}
            continue

        selected.add(event_id)
        reasons[event_id] = "selected"
        metadata[event_id] = {"selected": True}
        _increment(counts["topic"], topic)
        _increment(counts["content_class"], content_class)
        for group in candidate["source_groups"]:
            _increment(counts["source_group"], group)
        for source_id in candidate["source_ids"]:
            _increment(counts["source_id"], source_id)
    return selected, reasons, metadata


def _ai_rank(ai_client: Any, candidates: Sequence[dict[str, Any]]) -> dict[int, tuple[int, float | None]]:
    method = None
    for name in ("rank_events", "editorial_rank", "rank", "score_events"):
        candidate = getattr(ai_client, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None and callable(ai_client):
        method = ai_client
    if method is None:
        raise TypeError("editorial rank AI client does not expose rank_events/rank")
    payload = [
        {
            "event_id": int(row["event"].id),
            "title": row["event"].title,
            "summary_cn": row["event"].summary_cn,
            "topic": row["topic"],
            "content_class": row["content_class"],
            "source_group": row["source_group"],
            "display_score": row["display_score"],
        }
        for row in candidates
    ]
    try:
        raw = method(payload)
    except TypeError:
        raw = method(events=payload)
    rows = raw.get("rankings", raw.get("events", raw.get("scores", raw))) if isinstance(raw, Mapping) else raw
    if isinstance(rows, Mapping):
        rows = [{"event_id": key, **(value if isinstance(value, Mapping) else {"score": value})} for key, value in rows.items()]
    if not isinstance(rows, (list, tuple)):
        raise ValueError("editorial rank AI response must be a list or mapping")
    known = {int(row["event"].id) for row in candidates}
    result: dict[int, tuple[int, float | None]] = {}
    for position, row in enumerate(rows, start=1):
        if isinstance(row, (int, str)):
            event_id, ai_score = row, None
        elif isinstance(row, Mapping):
            event_id = row.get("event_id", row.get("id"))
            ai_score = row.get("score", row.get("rank_score", row.get("display_score")))
            position = _safe_int(row.get("rank", row.get("position", position)), position)
        else:
            continue
        try:
            event_id_int = int(event_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if event_id_int not in known:
            continue
        result[event_id_int] = (max(1, position), _float_or_none(ai_score))
    if not result:
        raise ValueError("editorial rank AI returned no known event ids")
    # Unmentioned events retain their deterministic position after mentioned
    # rows; they are not dropped from the daily selection.
    next_rank = max(value[0] for value in result.values()) + 1
    for row in candidates:
        event_id = int(row["event"].id)
        if event_id not in result:
            result[event_id] = (next_rank, None)
            next_rank += 1
    return result


def _paper_gate(event: IntelEvent) -> tuple[bool, str | None]:
    if str(event.topic or "").casefold() != "paper":
        return True, None
    flags = set(_json_strings(event.risk_flags_json))
    if "paper:arxiv_only" in flags:
        return False, "paper_gate:arxiv_only"
    if event.canonical_url and "arxiv.org" in event.canonical_url.casefold():
        return False, "paper_gate:arxiv_only"
    if any(flag in flags for flag in ("paper:unsupported", "paper:not_declared")):
        return False, "paper_gate:unsupported"
    supports: list[Mapping[str, Any]] = []
    raw_event = _json_value(event.resolution_raw_json, {})
    if isinstance(raw_event, Mapping):
        for key in ("paper_support", "paper", "paper_evidence"):
            value = raw_event.get(key)
            if isinstance(value, Mapping):
                supports.append(value)
    for relation in event.event_items:
        review = relation.item.ai_review if relation.item is not None else None
        # Wave 1 persists the normalized paper projection separately from the
        # provider raw payload.  Prefer that explicit field, while retaining
        # raw-response compatibility for historical rows.
        explicit_support = _json_value(
            getattr(review, "paper_support_json", None) if review is not None else None,
            {},
        )
        if isinstance(explicit_support, Mapping) and explicit_support:
            supports.append(explicit_support)
        raw_review = _json_value(review.raw_response_json, {}) if review is not None else {}
        if isinstance(raw_review, Mapping):
            value = raw_review.get("paper_support", raw_review.get("paper", raw_review.get("paper_evidence")))
            if isinstance(value, Mapping):
                supports.append(value)
        item_url = relation.item.canonical_url if relation.item is not None else None
        if item_url and "arxiv.org" in item_url.casefold():
            flags.add("paper:arxiv_only")
    if "paper:arxiv_only" in flags:
        return False, "paper_gate:arxiv_only"
    saw_arxiv_only_support = False
    for support in supports:
        support_arxiv_only = _paper_support_is_arxiv_only(support)
        saw_arxiv_only_support = saw_arxiv_only_support or support_arxiv_only
        if support_arxiv_only:
            continue
        if _paper_support_passes(support):
            return True, None
    if saw_arxiv_only_support:
        return False, "paper_gate:arxiv_only"
    return False, "paper_gate:unsupported"


def _paper_support_passes(support: Mapping[str, Any]) -> bool:
    """Evaluate the persisted PaperSupport projection without provider raw data.

    Wave 1 stores normalized evidence under ``evidence_url``/``evidence_links``
    and source booleans rather than the historical ``official_url``/``code_url``
    aliases.  Treat an explicit positive projection as sufficient evidence,
    while honoring explicit negative hard-gate/support/arXiv flags.
    """

    if not isinstance(support, Mapping):
        return False
    if _paper_support_is_arxiv_only(support):
        return False

    is_paper = support.get("is_paper", support.get("paper"))
    if is_paper is not None and not _coerce_bool(is_paper, False):
        return False

    hard_pass = support.get("hard_gate_pass")
    if hard_pass is not None:
        return _coerce_bool(hard_pass, False)

    supported = support.get(
        "supported",
        support.get("is_supported", support.get("eligible")),
    )
    if supported is not None and not _coerce_bool(supported, False):
        return False

    level = support.get("support_level", support.get("level", support.get("paper_support_level")))
    if level is not None:
        normalized_level = str(level).strip().casefold().replace("-", "_")
        if normalized_level not in {"supported", "strong", "pass", "true"}:
            return False

    evidence_url = _support_evidence_url(support)
    evidence_links = _support_links(support.get("evidence_links"))
    has_official_source = _coerce_bool(
        support.get("has_official_source", support.get("official")),
        False,
    )
    has_code = _coerce_bool(
        support.get("has_code", support.get("code_available")),
        False,
    )
    has_evidence = bool(evidence_url or evidence_links or has_official_source or has_code)
    if not has_evidence:
        return False

    return True


def _paper_support_is_arxiv_only(support: Mapping[str, Any]) -> bool:
    if not isinstance(support, Mapping):
        return False
    if _coerce_bool(support.get("arxiv_only", support.get("only_arxiv")), False):
        return True
    paper_url = _support_text(
        support.get(
            "paper_url",
            support.get("paper_link", support.get("url", support.get("arxiv_url"))),
        )
    )
    evidence_urls = _support_evidence_urls(support)
    evidence_links = _support_links(support.get("evidence_links"))
    non_arxiv_links = [
        link for link in (*evidence_urls, *evidence_links) if "arxiv.org" not in link.casefold()
    ]
    arxiv_links = [
        link for link in (*evidence_urls, *evidence_links) if "arxiv.org" in link.casefold()
    ]
    has_official_source = _coerce_bool(
        support.get("has_official_source", support.get("official")),
        False,
    )
    has_code = _coerce_bool(
        support.get("has_code", support.get("code_available")),
        False,
    )
    arxiv_paper = bool(paper_url and "arxiv.org" in paper_url.casefold())
    return (
        (arxiv_paper or bool(arxiv_links))
        and not non_arxiv_links
        and not (has_official_source or has_code)
    )


def _support_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _support_evidence_url(support: Mapping[str, Any]) -> str | None:
    values = _support_evidence_urls(support)
    return values[0] if values else None


def _support_evidence_urls(support: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in (
        "evidence_url",
        "support_url",
        "official_url",
        "official_x_url",
        "community_url",
        "code_url",
        "github_url",
        "source_url",
    ):
        value = _support_text(support.get(key))
        if value and value not in values:
            values.append(value)
    return values


def _support_links(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [link for raw in value if (link := _support_text(raw))]


def _preference_bonus(candidate: Mapping[str, Any], profile: EditorialProfile) -> float:
    # This is deliberately tiny: preferences break ties but do not overpower
    # an AI order or a materially different event display score.
    topic = str(candidate.get("topic") or "")
    preferred = profile.preferred_minima
    if isinstance(preferred.get("topic"), Mapping) and topic in preferred["topic"]:
        return 1.0
    if topic in preferred and not isinstance(preferred.get(topic), Mapping):
        return 1.0
    return 0.0


def _blocked_dimension(values: Iterable[str], counts: Mapping[str, int], caps: Mapping[str, int]) -> str | None:
    for value in values:
        cap = _cap(caps, value)
        if cap is not None and counts.get(value, 0) >= cap:
            return value
    return None


def _cap(caps: Mapping[str, int], key: str | None) -> int | None:
    if not caps:
        return None
    value = caps.get(str(key or ""), caps.get("*"))
    return value if value is not None else None


def _increment(counts: dict[str, int], key: str | None) -> None:
    if key is None:
        return
    text = str(key)
    counts[text] = counts.get(text, 0) + 1


def _first_mapping(data: Mapping[str, Any], nested: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if isinstance(value, Mapping):
            return value
        value = nested.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, raw in value.items():
        try:
            result[str(key)] = max(0, int(raw))
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _json_strings(value: Any) -> list[str]:
    raw = _json_value(value, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _number(value: Any) -> float:
    try:
        return max(0.0, float(str(value).replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
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


# Compatibility aliases used by scheduled jobs and tests.
run_rank_job = run_editorial_rank_job
run_editorial_rank = run_editorial_rank_job


__all__ = [
    "DEFAULT_TOPIC_CAPS",
    "EditorialProfile",
    "EditorialRankResult",
    "load_daily_profile",
    "run_editorial_rank",
    "run_editorial_rank_from_settings",
    "run_editorial_rank_job",
    "run_rank_job",
]
