from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ai.skills.stage_d_selection import STAGE_D_SELECTION_SCHEMA_VERSION
from app.jobs.stage_d_job import StageDExecutionError, StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent
from app.storage.repository import IntelRepository


REFERENCE = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _run_with_events(session_factory, *, count: int = 3) -> tuple[int, list[int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(
            edition_date="2026-08-19",
            reference_time=REFERENCE,
        )
        event_ids: list[int] = []
        for index in range(count):
            event = repo.upsert_event(
                run_id=build.id,
                event_key=f"event:{index}",
                canonical_url=f"https://example.test/{index}",
                title=f"候选事件 {index}",
                summary_cn=f"候选事件摘要 {index}",
                topic="model_release",
                display_score=90 - index,
                novelty_status="repeat" if index == count - 1 else "new",
                state="candidate",
                first_seen_at=REFERENCE,
                last_seen_at=REFERENCE,
            )
            event_ids.append(int(event.id))
        candidates = event_ids[:-1] if event_ids else []
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


class _Client:
    model = "selection-test"
    transport = "responses"
    max_retries = 0
    timeout_seconds = 1

    def __init__(self, *, error: BaseException | None = None):
        self.error = error
        self.calls: list[list[dict]] = []

    def select(self, events, *, edition, max_selected):
        self.calls.append([dict(event) for event in events])
        if self.error is not None:
            raise self.error
        selected = list(reversed(events[:max_selected]))
        return {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {
                    "event_id": int(event["event_id"]),
                    "reason_code": "balanced_daily",
                    "reason": "适合进入本期组合。",
                }
                for event in selected
            ],
            "unselected": [
                {
                    "event_id": int(event["event_id"]),
                    "reason_code": "lower_editorial_value",
                    "reason": "本期组合中优先级较低。",
                }
                for event in events
                if event not in selected
            ],
        }


def test_successful_run_task_is_reused_without_a_second_provider_call():
    session_factory = _db()
    run_id, _ = _run_with_events(session_factory)
    client = _Client()

    first = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)
    second = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert first.reused is False
    assert second.reused is True
    assert first.selected == second.selected == 2
    assert len(client.calls) == 1


def test_stage_d_reads_only_stage_c_candidate_event_ids():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory)
    client = _Client()

    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert result.candidates == 2
    assert [row["event_id"] for row in client.calls[0]] == event_ids[:-1]


def test_event_content_change_invalidates_stage_d_input_fingerprint():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory)
    client = _Client()
    run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    with session_factory() as session:
        event = session.get(IntelEvent, event_ids[0])
        assert event is not None
        event.title = "Stage C 重跑后的新标题"
        session.commit()

    rerun = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert rerun.reused is False
    assert len(client.calls) == 2
    assert client.calls[1][0]["title"] == "Stage C 重跑后的新标题"


def test_profile_change_invalidates_stage_d_config_fingerprint():
    session_factory = _db()
    run_id, _ = _run_with_events(session_factory)
    client = _Client()

    run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        profile=StageDProfile(max_selected=2),
    )
    rerun = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        profile=StageDProfile(max_selected=1),
    )

    assert rerun.reused is False
    assert rerun.selected == 1
    assert len(client.calls) == 2


def test_provider_failure_has_no_previous_snapshot_or_local_selection_fallback():
    session_factory = _db()
    run_id, _ = _run_with_events(session_factory)
    client = _Client(error=ValueError("selection provider unavailable"))

    with pytest.raises(StageDExecutionError, match="provider unavailable"):
        run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "blocked"
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None and task.status == "blocked"
        assert task.result.get("selected") is None


def test_stage_c_candidate_must_belong_to_the_current_build():
    session_factory = _db()
    run_id, _ = _run_with_events(session_factory)
    with session_factory() as session:
        repo = IntelRepository(session)
        cluster = repo.get_stage(run_id, "cluster")
        assert cluster is not None
        task = repo.get_task(cluster, subject_type="run", subject_id=run_id)
        assert task is not None
        task.result = {
            "current_event_ids": [9999],
            "candidate_event_ids": [9999],
        }
        session.commit()

    with pytest.raises(StageDExecutionError, match="missing from the current build"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=_Client(),
        )
