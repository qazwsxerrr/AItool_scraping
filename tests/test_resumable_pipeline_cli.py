from __future__ import annotations

from datetime import datetime, timezone

from typer.testing import CliRunner

from app import main
from app.config.settings import Settings
from app.jobs.fetch_job import IntelFetchResult
from app.jobs import pipeline_orchestrator as orchestrator
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelRepository


runner = CliRunner()


def test_pipeline_help_lists_formal_commands():
    result = runner.invoke(main.app, ["pipeline", "--help"])
    assert result.exit_code == 0
    for command in ("start", "stage-a", "stage-b", "stage-c", "rank", "export", "status", "retry", "resume", "adopt-existing"):
        assert command in result.stdout


def test_start_creates_and_freezes_scope(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")

    def fake_fetch(**kwargs):
        return IntelFetchResult(run_id=kwargs["run_id"])

    result = orchestrator.start_pipeline_run_from_settings(settings=settings, fetch_runner=fake_fetch)
    assert result.run_id > 0
    assert result.scope_frozen is True
    assert result.reference_time is not None

    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    with create_session_factory(engine)() as session:
        run = IntelRepository(session).session.get(orchestrator.IntelRun, result.run_id)
        assert run is not None
        assert run.scope_frozen is True
        assert run.reference_time is not None


def test_retry_stage_b_targets_only_stage_b_tasks(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run(reference_time=datetime.now(timezone.utc))
        stage = repo.ensure_stage(run.id, "analyze")
        task = repo.ensure_stage_task(stage, subject_type="run", subject_id=run.id)
        repo.fail_stage_task(task, error_code="provider_500", error_message="retry", retryable=True)
        session.commit()
        run_id, task_id = run.id, task.id

    seen: dict[str, object] = {}

    def fake_stage_b(**kwargs):
        seen.update(kwargs)
        return type("Result", (), {"errors": []})()

    monkeypatch.setattr(orchestrator, "run_pipeline_stage_b_from_settings", fake_stage_b)
    value = orchestrator.retry_pipeline_stage_from_settings(
        settings=settings,
        run_id=run_id,
        stage="stage-b",
    )
    assert value is not None
    assert seen["run_id"] == run_id
    assert seen["task_ids"] == [task_id]
