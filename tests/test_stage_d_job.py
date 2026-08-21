from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect

from app.ai.skills.stage_d_selection import (
    STAGE_D_SELECTION_SCHEMA_VERSION,
    strict_parse_stage_d_selection,
)
from app.jobs.stage_d_job import StageDExecutionError, StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelRunStageAttempt
from app.storage.repository import IntelRepository


REFERENCE = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return engine, create_session_factory(engine)


def _build(
    session_factory,
    *,
    event_count: int = 3,
    candidate_indexes: list[int] | None = None,
    needs_review_indexes: list[int] | None = None,
    with_cluster: bool = True,
) -> tuple[int, list[int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(
            edition_date="2026-08-19",
            reference_time=REFERENCE,
        )
        event_ids: list[int] = []
        needs_review = set(needs_review_indexes or ())
        for index in range(event_count):
            event = repo.upsert_event(
                run_id=build.id,
                event_key=f"event:{index}",
                canonical_url=f"https://example.test/events/{index}",
                title=f"Stage C 标题 {index}",
                summary_cn=f"Stage C 摘要 {index}",
                topic="model_release",
                topics=["model_release"],
                keywords=["模型", f"能力{index}"],
                entities=[{"name": f"Model {index}", "type": "product"}],
                content_class="official_model_company",
                source_group="official_blog",
                source_ids=["official-source"],
                source_groups=["official_blog"],
                display_score=90 - index,
                novelty_status="new",
                state="candidate",
                review_state="needs_review" if index in needs_review else "candidate",
                first_seen_at=REFERENCE,
                last_seen_at=REFERENCE,
            )
            event_ids.append(int(event.id))
        if with_cluster:
            candidates = (
                [event_ids[index] for index in candidate_indexes]
                if candidate_indexes is not None
                else event_ids
            )
            cluster = repo.ensure_stage(build.id, "cluster")
            task = repo.ensure_stage_task(
                cluster,
                subject_type="run",
                subject_id=build.id,
                target_run_id=build.id,
            )
            repo.complete_stage_task(
                task,
                result={
                    "current_event_ids": event_ids,
                    "candidate_event_ids": candidates,
                },
            )
            repo.finish_stage(cluster, status="succeeded")
        session.commit()
        return int(build.id), event_ids


class _SelectionClient:
    model = "stage-d-selection-test"
    transport = "responses"
    max_retries = 0
    timeout_seconds = 1

    def __init__(self, selected_indexes: list[int] | None = None, *, payload=None, error=None):
        self.selected_indexes = selected_indexes
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    def select(self, events, *, edition, max_selected):
        self.calls.append(
            {
                "events": [dict(event) for event in events],
                "edition": dict(edition),
                "max_selected": max_selected,
            }
        )
        if self.error is not None:
            raise self.error
        if self.payload is not None:
            return self.payload
        indexes = self.selected_indexes
        selected_events = list(events) if indexes is None else [events[index] for index in indexes]
        return {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {
                    "event_id": int(event["event_id"]),
                    "reason_code": "daily_value",
                    "reason": f"事件 {event['event_id']} 对本期读者有明确价值。",
                }
                for event in selected_events
            ],
        }


def test_selection_schema_accepts_only_an_ordered_candidate_subset():
    parsed = strict_parse_stage_d_selection(
        {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {"event_id": 3, "reason_code": "high_impact", "reason": "影响范围明确。"},
                {"event_id": 1, "reason_code": "actionable", "reason": "读者可立即使用。"},
            ],
        },
        candidate_event_ids=[1, 2, 3],
        max_selected=2,
    )

    assert [row.event_id for row in parsed.selected] == [3, 1]

    with pytest.raises(ValueError, match="unknown candidate"):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [{"event_id": 4, "reason_code": "impact", "reason": "未知事件。"}],
            },
            candidate_event_ids=[1, 2, 3],
            max_selected=2,
        )
    with pytest.raises(ValueError, match="duplicate event_id"):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {"event_id": 1, "reason_code": "impact", "reason": "理由一。"},
                    {"event_id": 1, "reason_code": "impact", "reason": "理由二。"},
                ],
            },
            candidate_event_ids=[1, 2, 3],
            max_selected=2,
        )
    with pytest.raises(ValueError, match="exceeding max_selected"):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {"event_id": 1, "reason_code": "impact", "reason": "理由一。"},
                    {"event_id": 2, "reason_code": "impact", "reason": "理由二。"},
                ],
            },
            candidate_event_ids=[1, 2, 3],
            max_selected=1,
        )
    with pytest.raises(ValueError):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {
                        "event_id": 1,
                        "reason_code": "impact",
                        "reason": "理由。",
                        "display_title_zh": "Stage D 不得改标题",
                    }
                ],
            },
            candidate_event_ids=[1],
            max_selected=1,
        )


def test_stage_d_requires_a_successful_stage_c_candidate_contract():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, with_cluster=False)

    with pytest.raises(StageDExecutionError, match="Stage C cluster stage must succeed"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=_SelectionClient(),
        )

    with session_factory() as session:
        assert IntelRepository(session).get_stage(run_id, "stage_d") is None


def test_empty_stage_c_candidates_finish_without_calling_the_provider():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, event_count=0)
    client = _SelectionClient(error=AssertionError("provider must not be called"))

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
    )

    assert (result.candidates, result.selected, result.unselected) == (0, 0, 0)
    assert client.calls == []
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "succeeded"
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None
        assert task.result == {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "candidate_event_ids": [],
            "withheld_needs_review_event_ids": [],
            "all_stage_c_candidate_event_ids": [],
            "selected": [],
            "input_fingerprint": task.input_fingerprint,
            "config_fingerprint": task.config_fingerprint,
            "provider_attempts": 0,
        }


def test_stage_d_persists_only_the_ordered_selection_task_result():
    engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, candidate_indexes=[2, 0])
    client = _SelectionClient(selected_indexes=[1])

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
    )

    assert (result.candidates, result.selected, result.unselected) == (2, 1, 1)
    assert [row["event_id"] for row in client.calls[0]["events"]] == [event_ids[2], event_ids[0]]
    assert client.calls[0]["max_selected"] == 30
    assert "intel_event_stage_d_snapshots" not in inspect(engine).get_table_names()
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "succeeded"
        tasks = repo.list_stage_tasks(stage, include_expired=True)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.result["candidate_event_ids"] == [event_ids[2], event_ids[0]]
        assert task.result["selected"] == [
            {
                "event_id": event_ids[0],
                "reason_code": "daily_value",
                "reason": f"事件 {event_ids[0]} 对本期读者有明确价值。",
            }
        ]
        assert set(task.result) == {
            "schema_version",
            "candidate_event_ids",
            "withheld_needs_review_event_ids",
            "all_stage_c_candidate_event_ids",
            "selected",
            "input_fingerprint",
            "config_fingerprint",
            "provider_attempts",
        }
        event = session.get(IntelEvent, event_ids[0])
        assert event is not None and event.title == "Stage C 标题 0"
        attempts = repo.list_stage_attempts(task)
        assert len(attempts) == 1
        assert isinstance(attempts[0], IntelRunStageAttempt)
        assert attempts[0].raw_response["schema_version"] == STAGE_D_SELECTION_SCHEMA_VERSION


def test_stage_d_withholds_needs_review_events_from_the_provider_and_selection():
    _engine, session_factory = _db()
    run_id, event_ids = _build(
        session_factory,
        event_count=2,
        needs_review_indexes=[1],
    )
    client = _SelectionClient()

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
    )

    assert (result.candidates, result.withheld_needs_review, result.selected) == (1, 1, 1)
    assert [row["event_id"] for row in client.calls[0]["events"]] == [event_ids[0]]
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None
        assert task.result["candidate_event_ids"] == [event_ids[0]]
        assert task.result["withheld_needs_review_event_ids"] == [event_ids[1]]
        assert task.result["all_stage_c_candidate_event_ids"] == event_ids


def test_invalid_selection_is_blocked_without_local_fallback():
    _engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, event_count=1)
    client = _SelectionClient(
        payload={
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {
                    "event_id": event_ids[0],
                    "reason_code": "impact",
                    "reason": "选择该事件。",
                    "title": "Stage D 试图覆盖 Stage C 标题",
                }
            ],
        }
    )

    with pytest.raises(StageDExecutionError, match="selection"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=client,
        )

    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "blocked"
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None and task.status == "blocked"
        assert task.result.get("selected") is None
        attempts = repo.list_stage_attempts(task)
        assert attempts[0].raw_response["selected"][0]["title"] == "Stage D 试图覆盖 Stage C 标题"


def test_profile_enforces_the_only_stage_d_limit():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, event_count=2)

    with pytest.raises(StageDExecutionError, match="max_selected=1"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=_SelectionClient(),
            profile=StageDProfile(max_selected=1),
        )


def test_zero_selection_limit_finishes_without_calling_the_provider():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, event_count=2)
    client = _SelectionClient(error=AssertionError("provider must not be called"))

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        profile=StageDProfile(max_selected=0),
    )

    assert (result.candidates, result.selected, result.unselected) == (2, 0, 2)
    assert client.calls == []
