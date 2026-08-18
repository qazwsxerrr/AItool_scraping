from __future__ import annotations

from datetime import datetime, timezone

from typer.testing import CliRunner

from app import main
from app.config.limits import RECENT_WINDOW_HOURS
from app.config.settings import Settings
from app.jobs.fetch_job import IntelFetchResult
from app.jobs import pipeline_orchestrator as orchestrator
from app.jobs.stage_a_screen_job import StageAScreenResult
from app.jobs.stage_b_analysis_job import StageBAnalysisResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelRepository


runner = CliRunner()


def test_pipeline_help_lists_formal_commands():
    result = runner.invoke(main.app, ["pipeline", "--help"])
    assert result.exit_code == 0
    for command in ("run", "start", "stage-a", "stage-b", "stage-c", "stage-d", "export", "status", "retry", "resume", "adopt-existing"):
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
        assert run.scope["freshness_window_hours"] == RECENT_WINDOW_HOURS
        assert run.scope["freshness_undated_policy"] == "exclude"
        assert run.scope["freshness_github_trending_policy"] == "exempt"


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


def test_stage_wrappers_forward_configured_ai_concurrency(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}", ai_review_concurrency=7)
    seen: dict[str, int] = {}

    monkeypatch.setattr(orchestrator, "_registry", lambda *args, **kwargs: {})

    def fake_screen(**kwargs):
        seen["screen"] = kwargs["concurrency"]
        return StageAScreenResult(run_id=1)

    def fake_analysis(**kwargs):
        seen["analysis"] = kwargs["concurrency"]
        return StageBAnalysisResult(run_id=1)

    monkeypatch.setattr(orchestrator, "run_stage_a_screen_job", fake_screen)
    monkeypatch.setattr(orchestrator, "run_stage_b_analysis_job", fake_analysis)
    orchestrator.run_pipeline_stage_a_from_settings(settings=settings, run_id=1, ai_client=object())
    orchestrator.run_pipeline_stage_b_from_settings(settings=settings, run_id=1, ai_client=object())
    assert seen == {"screen": 7, "analysis": 7}


def test_manual_pipeline_run_status_finalizes_only_after_export(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run(reference_time=datetime.now(timezone.utc))
        for stage_name in ("fetch", "screen", "analyze", "cluster", "stage_d"):
            stage = repo.ensure_stage(run.id, stage_name)
            repo.finish_stage(stage, status="succeeded")
        session.commit()
        run_id = int(run.id)

    assert orchestrator._sync_pipeline_run_status(session_factory, run_id, finalize=False) == "running"
    with session_factory() as session:
        assert session.get(orchestrator.IntelRun, run_id).status == "running"

    with session_factory() as session:
        repo = IntelRepository(session)
        export_stage = repo.ensure_stage(run_id, "export")
        repo.finish_stage(export_stage, status="succeeded")
        session.commit()

    assert orchestrator._sync_pipeline_run_status(session_factory, run_id, finalize=True) == "completed"
    with session_factory() as session:
        run = session.get(orchestrator.IntelRun, run_id)
        assert run.status == "completed"
        assert run.finished_at is not None


def test_completed_force_rerun_clears_stale_ai_limit_partial_marker(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run(reference_time=datetime.now(timezone.utc))
        for stage_name in orchestrator.PIPELINE_STAGE_ORDER:
            stage = repo.ensure_stage(run.id, stage_name)
            repo.finish_stage(stage, status="succeeded")
        repo.finish_run(
            run.id,
            status="partial",
            partial=True,
            partial_reason="ai_limit:1000",
        )
        session.commit()
        run_id = int(run.id)

    assert orchestrator._sync_pipeline_run_status(
        session_factory,
        run_id,
        finalize=True,
        partial=False,
    ) == "completed"
    with session_factory() as session:
        run = session.get(orchestrator.IntelRun, run_id)
        assert run.status == "completed"
        assert run.partial is False
        assert run.partial_reason in {None, ""}


def test_pipeline_run_auto_creates_and_reuses_run_id(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")
    seen: dict[str, object] = {}
    announced: list[int] = []
    start = orchestrator.PipelineStartResult(
        run_id=42,
        fetch=IntelFetchResult(run_id=42),
        reference_time=datetime.now(timezone.utc),
        scope_frozen=True,
    )
    resume = orchestrator.PipelineResumeResult(run_id=42, ran_stages=["screen", "analyze", "cluster", "stage_d", "export"])

    def fake_start(**kwargs):
        seen["start_limit"] = kwargs["limit"]
        seen["force"] = kwargs["force"]
        return start

    def fake_resume(**kwargs):
        seen["resume_run_id"] = kwargs["run_id"]
        seen["resume_limit"] = kwargs["limit"]
        seen["output_dir"] = kwargs["output_dir"]
        return resume

    monkeypatch.setattr(orchestrator, "start_pipeline_run_from_settings", fake_start)
    monkeypatch.setattr(orchestrator, "resume_pipeline_from_settings", fake_resume)
    monkeypatch.setattr(
        orchestrator,
        "pipeline_status_from_settings",
        lambda **kwargs: orchestrator.PipelineStatus(
            run_id=kwargs["run_id"],
            run_status="completed",
            reference_time=None,
            scope_frozen=True,
        ),
    )

    result = orchestrator.run_pipeline_from_settings(
        settings=settings,
        limit=30,
        force=True,
        output_dir=tmp_path / "out",
        on_start=lambda value: announced.append(value.run_id),
    )

    assert result.run_id == 42
    assert announced == [42]
    assert seen == {
        "start_limit": 30,
        "force": True,
        "resume_run_id": 42,
        "resume_limit": None,
        "output_dir": tmp_path / "out",
    }


def test_partial_stage_successes_allow_downstream_and_finalize_partial(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        repo = IntelRepository(session)
        run = repo.start_run(reference_time=datetime.now(timezone.utc))
        fetch = repo.ensure_stage(run.id, "fetch")
        repo.finish_stage(fetch, status="failed", error_code="source_failed")
        screen = repo.ensure_stage(run.id, "screen")
        for index, status in enumerate(("succeeded", "succeeded"), start=1):
            task = repo.ensure_stage_task(screen, subject_type="item", subject_id=index, item_id=index)
            task.status = status
        analyze = repo.ensure_stage(run.id, "analyze")
        for index, status in enumerate(("succeeded", "retry_waiting"), start=10):
            task = repo.ensure_stage_task(analyze, subject_type="item", subject_id=index, item_id=index)
            task.status = status
        for stage_name in ("screen", "analyze", "cluster", "stage_d", "export"):
            stage = repo.get_stage(run.id, stage_name)
            if stage is not None:
                repo.refresh_stage_status(stage)
        session.commit()
        run_id = int(run.id)

    assert orchestrator._stage_needs_resume(session_factory, run_id, "cluster") is True

    with session_factory() as session:
        repo = IntelRepository(session)
        cluster = repo.ensure_stage(run_id, "cluster")
        cluster_task = repo.ensure_stage_task(
            cluster, subject_type="run", subject_id=run_id, target_run_id=run_id
        )
        cluster_task.status = "succeeded"
        repo.refresh_stage_status(cluster)
        session.commit()

    assert orchestrator._stage_needs_resume(session_factory, run_id, "stage_d") is True

    with session_factory() as session:
        repo = IntelRepository(session)
        for stage_name in ("stage_d", "export"):
            stage = repo.ensure_stage(run_id, stage_name)
            task = repo.ensure_stage_task(stage, subject_type="run", subject_id=run_id, target_run_id=run_id)
            task.status = "succeeded"
            repo.refresh_stage_status(stage)
        session.commit()

    assert orchestrator._sync_pipeline_run_status(session_factory, run_id, finalize=True) == "partial"
    with session_factory() as session:
        run = session.get(orchestrator.IntelRun, run_id)
        assert run.status == "partial"
        assert run.finished_at is not None
