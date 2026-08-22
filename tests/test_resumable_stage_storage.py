from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect

from app.storage.db import STATE_TABLE_NAMES, create_engine_from_url, create_session_factory, init_db
from app.storage.models import Base, IntelRunStageAttempt
from app.storage.repository import IntelRepository


def _factory(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'intel.db'}")
    init_db(engine)
    return create_session_factory(engine), engine


def _new_draft(repo: IntelRepository, *, reference: datetime | None = None):
    _, build = repo.start_daily_build(
        edition_date="2026-08-19",
        reference_time=reference,
    )
    return build


def test_existing_complete_schema_gains_only_coordinator_state_tables(tmp_path):
    database = tmp_path / "before-state.db"
    engine = create_engine_from_url(f"sqlite:///{database}")
    Base.metadata.create_all(
        engine,
        tables=[table for name, table in Base.metadata.tables.items() if name not in STATE_TABLE_NAMES],
    )
    init_db(engine)

    assert STATE_TABLE_NAMES <= set(inspect(engine).get_table_names())


def test_daily_build_persists_explicit_date_and_reference_time(tmp_path):
    session_factory, engine = _factory(tmp_path)
    reference = datetime(2026, 8, 19, 12, 30, tzinfo=timezone.utc)
    with session_factory() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(edition_date="2026-08-19", reference_time=reference)
        session.commit()
        assert build.reference_time == reference
        assert build.edition_date == "2026-08-19"
        assert "edition_date" not in build.scope
        assert "edition_timezone" not in build.scope

    indexes = {index["name"] for index in inspect(engine).get_indexes("daily_editions")}
    assert "ix_daily_editions_status" in indexes


def test_task_claim_lease_retry_and_immutable_raw_attempt(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        build = _new_draft(repo)
        stage = repo.ensure_stage(build.id, "analyze", config_fingerprint="cfg-v1")
        task = repo.ensure_stage_task(
            stage,
            subject_type="item",
            subject_id="7",
            input_fingerprint="item-v1",
            config_fingerprint="cfg-v1",
        )
        claimed = repo.claim_stage_task(stage, task_id=task.id, owner="worker-a", lease_seconds=60)
        assert claimed is not None
        assert claimed.status == "running"
        attempt_id = claimed.last_attempt_id
        repo.complete_stage_task(claimed, owner="worker-a", raw_response={"answer": 1}, result={"ok": True})
        assert claimed.status == "succeeded"
        attempt = session.get(IntelRunStageAttempt, attempt_id)
        assert attempt is not None
        first_raw = attempt.raw_response_json
        repo.finish_stage_attempt(attempt, status="succeeded", raw_response={"answer": 2})
        assert attempt.raw_response_json == first_raw
        assert repo.task_is_reusable(claimed, input_fingerprint="item-v1", config_fingerprint="cfg-v1")
        assert not repo.task_is_reusable(claimed, input_fingerprint="item-v2", config_fingerprint="cfg-v1")

        retry_task = repo.ensure_stage_task(stage, subject_type="item", subject_id="8")
        retry_claim = repo.claim_stage_task(stage, task_id=retry_task.id, owner="worker-a")
        repo.fail_stage_task(retry_claim, error_code="429", error_message="rate limited", owner="worker-a")
        assert retry_task.status == "retry_waiting"
        assert repo.retry_failed(stage) == [retry_task]
        assert retry_task.status == "pending"


def test_expired_task_lease_is_recoverable_without_losing_attempt(tmp_path):
    session_factory, _ = _factory(tmp_path)
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        repo = IntelRepository(session)
        build = _new_draft(repo, reference=now)
        stage = repo.ensure_stage(build.id, "screen")
        task = repo.ensure_stage_task(stage, subject_id="9")
        claimed = repo.claim_stage_task(stage, task_id=task.id, owner="worker-a", lease_seconds=1, now=now)
        assert claimed is not None
        recovered = repo.recover_expired_stage_tasks(stage, now=now + timedelta(seconds=2))
        assert recovered == [task]
        assert task.status == "retry_waiting"
        assert len(repo.list_stage_attempts(task)) == 1
        assert repo.list_stage_attempts(task)[0].error_code == "lease_expired"


def test_build_scope_freeze_is_idempotent_but_rejects_new_membership(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        build = _new_draft(repo)
        repo.freeze_run_scope(build.id, source_ids=["source-a"])
        assert build.scope_frozen is True
        assert build.scope_frozen_at is not None
        assert repo.freeze_run_scope(build.id).id == build.id
        with pytest.raises(RuntimeError, match="already frozen"):
            repo.set_run_scope(build.id, source_ids=["source-b"])


def test_changed_fingerprint_and_force_reclaim_successful_task(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        build = _new_draft(repo)
        stage = repo.ensure_stage(build.id, "analyze", config_fingerprint="cfg-v1")
        task = repo.ensure_stage_task(stage, subject_id="item-x", input_fingerprint="item-v1")
        first = repo.claim_stage_task(stage, task_id=task.id, owner="worker-a")
        assert first is not None
        repo.complete_stage_task(first, owner="worker-a", result={"version": 1})
        assert repo.claim_stage_task(
            stage,
            task_id=task.id,
            owner="worker-a",
            input_fingerprint="item-v2",
        ) is not None
        repo.complete_stage_task(task, owner="worker-a", result={"version": 2})
        assert repo.claim_stage_task(stage, task_id=task.id, owner="worker-a") is None
        assert repo.claim_stage_task(stage, task_id=task.id, owner="worker-a", force=True) is not None


def test_force_can_create_a_new_stage_after_downstream_invalidation(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        build = _new_draft(repo)
        stage = repo.ensure_stage(build.id, "stage_d")
        repo.ensure_stage_task(stage, subject_type="run", subject_id=build.id)
        repo.invalidate_downstream_stages(
            build.id,
            stage_names=("stage_d",),
            upstream_stage="cluster",
        )

        recreated = repo.ensure_stage(build.id, "stage_d", force=True)

        assert recreated.id is not None
        assert repo.get_stage(build.id, "stage_d") is recreated
