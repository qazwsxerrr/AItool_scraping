from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, select, text

from app.storage.db import (
    STATE_TABLE_NAMES,
    _ensure_intel_run_edition_date_index,
    _upgrade_intel_run_edition_date,
    create_engine_from_url,
    create_session_factory,
    init_db,
)
from app.storage.models import AIItemReview, Base, IntelItem, IntelRun, IntelRunStageAttempt, Source
from app.storage.repository import IntelRepository


def _factory(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'intel.db'}")
    init_db(engine)
    return create_session_factory(engine), engine


def test_existing_complete_schema_gains_only_new_state_tables(tmp_path):
    database = tmp_path / "before-resume.db"
    engine = create_engine_from_url(f"sqlite:///{database}")
    from app.storage.models import Base

    Base.metadata.create_all(
        engine,
        tables=[table for name, table in Base.metadata.tables.items() if name not in STATE_TABLE_NAMES],
    )
    init_db(engine)
    assert STATE_TABLE_NAMES <= set(inspect(engine).get_table_names())


def test_start_run_freezes_reference_time_in_legacy_scope(tmp_path):
    session_factory, _ = _factory(tmp_path)
    reference = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)
    with session_factory() as session:
        run = IntelRepository(session).start_run(reference_time=reference)
        assert run.reference_time == reference
        session.commit()


def test_start_run_persists_an_indexed_daily_edition_date(tmp_path):
    session_factory, engine = _factory(tmp_path)
    reference = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        run = IntelRepository(session).start_run(reference_time=reference)
        session.commit()
        persisted = session.get(IntelRun, run.id)
        assert persisted is not None
        assert persisted.edition_date == "2026-08-17"
        assert persisted._edition_date == date(2026, 8, 17)

    indexes = {index["name"] for index in inspect(engine).get_indexes("intel_runs")}
    assert "ix_intel_runs_edition_date" in indexes


def test_additive_edition_date_upgrade_backfills_a_complete_legacy_run_table(tmp_path):
    """The narrow migration only accepts a table missing this one column."""

    database = tmp_path / "pre-edition-column.db"
    engine = create_engine_from_url(f"sqlite:///{database}")
    table = Base.metadata.tables["intel_runs"]
    legacy_columns = [column.name for column in table.columns if column.name != "edition_date"]
    definitions = ["id INTEGER PRIMARY KEY"] + [f"{name} TEXT" for name in legacy_columns if name != "id"]
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE intel_runs ({', '.join(definitions)})"))
        connection.execute(
            text(
                "INSERT INTO intel_runs (id, scope_json, started_at) "
                "VALUES (1, :scope_json, :started_at)"
            ),
            {
                "scope_json": '{"reference_time":"2026-08-16T16:00:00+00:00"}',
                "started_at": "2026-08-16T16:00:00+00:00",
            },
        )

    _upgrade_intel_run_edition_date(engine)
    _ensure_intel_run_edition_date_index(engine)

    with engine.connect() as connection:
        edition_date = connection.execute(
            text("SELECT edition_date FROM intel_runs WHERE id = 1")
        ).scalar_one()
    assert str(edition_date) == "2026-08-17"
    indexes = {index["name"] for index in inspect(engine).get_indexes("intel_runs")}
    assert "ix_intel_runs_edition_date" in indexes


def test_task_claim_lease_retry_and_immutable_raw_attempt(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run()
        stage = repo.ensure_stage(run.id, "analyze", config_fingerprint="cfg-v1")
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
        session.commit()


def test_expired_task_lease_is_recoverable_without_losing_attempt(tmp_path):
    session_factory, _ = _factory(tmp_path)
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run(reference_time=now)
        stage = repo.ensure_stage(run.id, "screen")
        task = repo.ensure_stage_task(stage, subject_id="9")
        claimed = repo.claim_stage_task(stage, task_id=task.id, owner="worker-a", lease_seconds=1, now=now)
        assert claimed is not None
        recovered = repo.recover_expired_stage_tasks(stage, now=now + timedelta(seconds=2))
        assert recovered == [task]
        assert task.status == "retry_waiting"
        assert len(repo.list_stage_attempts(task)) == 1
        assert repo.list_stage_attempts(task)[0].error_code == "lease_expired"


def test_adopt_existing_requires_projection_run_match_and_never_invents_history(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        source = Source(
            id="source-1",
            name="Source",
            transport="rss",
            url="https://example.test/feed",
            content_class="community_social",
        )
        session.add(source)
        session.flush()
        run = repo.start_run()
        wrong_run = repo.start_run()
        item = IntelItem(
            source_id=source.id,
            title="Item",
            content_class="community_social",
            content_hash="a" * 64,
        )
        session.add(item)
        session.flush()
        repo.record_run_item(run.id, item.id)
        review = AIItemReview(
            item_id=item.id,
            run_id=wrong_run.id,
            content_class=item.content_class,
            status="success",
        )
        session.add(review)
        session.flush()
        assert repo.adopt_existing_stage_tasks(run.id, "analyze") == []
        review.run_id = run.id
        session.flush()
        adopted = repo.adopt_existing_stage_tasks(run.id, "analyze")
        assert len(adopted) == 1
        assert adopted[0].status == "succeeded"
        session.commit()


def test_run_scope_freeze_is_idempotent_but_rejects_new_membership(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run(source_ids=["source-a"])
        repo.freeze_run_scope(run.id)
        assert run.scope_frozen is True
        assert run.scope_frozen_at is not None
        assert repo.freeze_run_scope(run.id).id == run.id
        with pytest.raises(RuntimeError, match="already frozen"):
            repo.set_run_scope(run.id, source_ids=["source-b"])


def test_changed_fingerprint_and_force_reclaim_successful_task(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run()
        stage = repo.ensure_stage(run.id, stage="analyze", config_fingerprint="cfg-v1")
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
