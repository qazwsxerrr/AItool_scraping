from __future__ import annotations

from datetime import datetime, timezone

from typer.testing import CliRunner

from app import main
from app.config.settings import Settings
from app.jobs.fetch_job import IntelFetchResult, IntelSourceStats
from app.jobs import pipeline_orchestrator as orchestrator
from app.jobs.stage_a_screen_job import StageAScreenResult
from app.jobs.stage_b_analysis_job import StageBAnalysisResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.draft_workspace import draft_database_url
from app.storage.repository import IntelRepository


runner = CliRunner()


def test_pipeline_help_lists_only_date_addressed_formal_commands():
    result = runner.invoke(main.app, ["pipeline", "--help"])

    assert result.exit_code == 0
    for command in ("run", "start", "stage-a", "stage-b1", "stage-c", "stage-d", "export", "status", "retry", "resume"):
        assert command in result.stdout
    assert "adopt-existing" not in result.stdout
    assert "run-id" not in result.stdout


def test_start_creates_and_freezes_an_isolated_daily_draft(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")

    def fake_fetch(**kwargs):
        return IntelFetchResult(run_id=kwargs["run_id"])

    result = orchestrator.start_pipeline_run_from_settings(
        settings=settings,
        edition_date="2026-08-19",
        fetch_runner=fake_fetch,
    )

    assert result.edition_date == "2026-08-19"
    assert result.scope_frozen is True
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    with create_session_factory(public_engine)() as session:
        repo = IntelRepository(session)
        assert repo.get_daily_edition("2026-08-19") is None

    draft_engine = create_engine_from_url(draft_database_url(settings.database_url, "2026-08-19"))
    with create_session_factory(draft_engine)() as session:
        repo = IntelRepository(session)
        edition = repo.get_daily_edition("2026-08-19")
        draft = repo.draft_run_for_edition("2026-08-19")
        assert edition is not None and draft is not None
        assert draft.scope_frozen is True
        assert draft.reference_time is not None
        assert draft.scope["freshness_window_hours"] is None
        assert draft.scope["freshness_cutoff_mode"] == "edition_previous_day_midnight"
        assert draft.scope["freshness_timezone"] == "Asia/Shanghai"
        assert draft.scope["freshness_edition_date"] == "2026-08-19"
        assert draft.scope["freshness_undated_policy"] == "exclude"
        assert draft.scope["freshness_github_trending_policy"] == "exempt"


def test_failed_fetch_is_recorded_as_a_warning_without_blocking_the_build(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")

    def fake_fetch(**kwargs):
        return IntelFetchResult(
            run_id=kwargs["run_id"],
            stats={"broken_source": IntelSourceStats(source_id="broken_source", failed=1, status="failed")},
        )

    result = orchestrator.start_pipeline_run_from_settings(
        settings=settings,
        edition_date="2026-08-19",
        fetch_runner=fake_fetch,
    )

    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    with create_session_factory(public_engine)() as session:
        repo = IntelRepository(session)
        assert repo.get_daily_edition("2026-08-19") is None

    draft_engine = create_engine_from_url(draft_database_url(settings.database_url, "2026-08-19"))
    with create_session_factory(draft_engine)() as session:
        repo = IntelRepository(session)
        edition = repo.get_daily_edition("2026-08-19")
        draft = repo.draft_run_for_edition("2026-08-19")
        assert edition is not None and draft is not None
        assert int(draft.id) == int(result.run_id)
        assert edition.status == "building"
        assert edition.error is None
        assert draft.status == "running"
        assert draft.error is None
        assert draft.partial is False
        assert draft.partial_reason is None
        assert draft.finished_at is None

    status = orchestrator.pipeline_edition_status_from_settings(
        settings=settings,
        edition_date="2026-08-19",
    )
    fetch = next(row for row in status.stages if row["stage"] == "fetch")
    assert fetch["status"] == "succeeded"
    assert fetch["total"] == 1
    assert fetch["failed"] == 1


def test_retry_stage_b1_targets_only_current_draft_stage_b1_tasks(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        repo = IntelRepository(session)
        _, draft = repo.start_daily_build(edition_date="2026-08-19", reference_time=datetime.now(timezone.utc))
        stage = repo.ensure_stage(draft.id, "analyze")
        task = repo.ensure_stage_task(stage, subject_type="item", subject_id=draft.id)
        repo.fail_stage_task(task, error_code="provider_500", error_message="retry", retryable=True)
        session.commit()
        task_id = int(task.id)

    seen: dict[str, object] = {}

    def fake_stage_b(**kwargs):
        seen.update(kwargs)
        return type("Result", (), {"errors": []})()

    monkeypatch.setattr(orchestrator, "run_pipeline_stage_b_from_settings", fake_stage_b)
    value = orchestrator.retry_pipeline_stage_from_settings(
        settings=settings,
        run_id=draft.id,
        stage="stage-b1",
    )

    assert value is not None
    assert seen["run_id"] == draft.id
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


def test_pipeline_run_returns_public_edition_status_while_build_id_stays_internal(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")
    seen: dict[str, object] = {}
    announced: list[str | None] = []
    start = orchestrator.PipelineStartResult(
        run_id=42,
        fetch=IntelFetchResult(run_id=42),
        reference_time=datetime.now(timezone.utc),
        scope_frozen=True,
        edition_date="2026-08-19",
    )
    resume = orchestrator.PipelineResumeResult(run_id=42, ran_stages=["screen", "analyze", "cluster", "stage_d", "export"])

    def fake_start(**kwargs):
        seen["edition_date"] = kwargs["edition_date"]
        seen["limit"] = kwargs["limit"]
        return start

    def fake_resume(**kwargs):
        seen["resume_run_id"] = kwargs["run_id"]
        return resume

    monkeypatch.setattr(orchestrator, "start_pipeline_run_from_settings", fake_start)
    monkeypatch.setattr(orchestrator, "resume_pipeline_from_settings", fake_resume)
    monkeypatch.setattr(
        orchestrator,
        "pipeline_edition_status_from_settings",
        lambda **kwargs: orchestrator.DailyEditionStatus(
            edition_date=kwargs["edition_date"],
            status="published",
        ),
    )

    result = orchestrator.run_pipeline_from_settings(
        settings=settings,
        edition_date="2026-08-19",
        limit=30,
        output_dir=tmp_path / "out",
        on_start=lambda value: announced.append(value.edition_date),
    )

    assert result.status == "published"
    assert announced == ["2026-08-19"]
    assert seen == {"edition_date": "2026-08-19", "limit": 30, "resume_run_id": 42}
