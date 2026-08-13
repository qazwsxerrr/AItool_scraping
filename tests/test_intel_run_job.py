from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.jobs import run_job
from app.jobs.ai_review_job import AIReviewResult
from app.jobs.export_job import IntelExportResult, run_intel_export_job
from app.jobs.fetch_job import IntelFetchResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, IntelRun, Source
from sqlalchemy import select


def _settings(path: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{path}")


def test_run_once_passes_one_run_id_through_fetch_and_finishes_run(tmp_path, monkeypatch):
    db_path = tmp_path / "run.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    calls = {}

    def fake_fetch(**kwargs):
        calls["fetch_run_id"] = kwargs.get("run_id")
        return IntelFetchResult(run_id=kwargs.get("run_id"), stats={})

    def fake_ai_review(**kwargs):
        calls["ai_review_source"] = kwargs.get("source_filter")
        calls["ai_review_run_id"] = kwargs.get("run_id")
        return AIReviewResult(run_id=kwargs.get("run_id"))

    def fake_export(**kwargs):
        return IntelExportResult(0, 0, "items", "digest", "pending")

    monkeypatch.setattr(run_job, "run_intel_fetch_from_settings", fake_fetch)
    monkeypatch.setattr(run_job, "run_ai_review_from_settings", fake_ai_review)
    monkeypatch.setattr(run_job, "run_intel_export_from_settings", fake_export)

    result = run_job.run_intel_once_from_settings(
        settings=_settings(db_path),
        source="github_source",
        content_class="project_tool",
        limit=5,
    )
    assert result.status == "completed"
    assert result.run_id == calls["fetch_run_id"]
    assert calls["ai_review_source"] == "github_source"
    assert calls["ai_review_run_id"] == result.run_id
    with create_session_factory(engine)() as session:
        row = session.scalar(select(IntelRun))
        assert row.status == "completed"


def test_run_once_ai_only_path_never_enters_legacy_verifier(tmp_path, monkeypatch):
    db_path = tmp_path / "ai-only-run.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)

    def forbidden(*args, **kwargs):
        raise AssertionError("run-once must not invoke legacy process verification")

    monkeypatch.setattr("app.jobs.process_job._verify", forbidden)
    monkeypatch.setattr(
        run_job,
        "run_intel_fetch_from_settings",
        lambda **kwargs: IntelFetchResult(run_id=kwargs.get("run_id"), stats={}),
    )
    monkeypatch.setattr(
        run_job,
        "run_ai_review_from_settings",
        lambda **kwargs: AIReviewResult(run_id=kwargs.get("run_id"), selected=1, analyzed=1),
    )
    monkeypatch.setattr(
        run_job,
        "run_intel_export_from_settings",
        lambda **kwargs: IntelExportResult(1, 0, "items", "digest", "pending"),
    )

    result = run_job.run_intel_once_from_settings(settings=_settings(db_path), limit=1)

    assert result.status == "completed"
    assert result.ai_review.selected == 1


def test_run_once_dry_run_does_not_create_database(tmp_path, monkeypatch):
    db_path = tmp_path / "dry.db"
    monkeypatch.setattr(
        run_job,
        "run_intel_fetch_from_settings",
        lambda **kwargs: IntelFetchResult(dry_run=True),
    )
    monkeypatch.setattr(
        run_job,
        "run_ai_review_from_settings",
        lambda **kwargs: AIReviewResult(run_id=kwargs.get("run_id")),
    )
    monkeypatch.setattr(
        run_job,
        "run_intel_export_from_settings",
        lambda **kwargs: IntelExportResult(0, 0, "items", "digest", "pending", dry_run=True),
    )
    result = run_job.run_intel_once_from_settings(settings=_settings(db_path), dry_run=True)
    assert result.status == "dry_run"
    assert not db_path.exists()


def test_run_once_marks_isolated_ai_failures_as_completed_with_errors(tmp_path, monkeypatch):
    db_path = tmp_path / "ai-failed.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)

    monkeypatch.setattr(
        run_job,
        "run_intel_fetch_from_settings",
        lambda **kwargs: IntelFetchResult(run_id=kwargs.get("run_id"), stats={}),
    )
    monkeypatch.setattr(
        run_job,
        "run_ai_review_from_settings",
        lambda **kwargs: AIReviewResult(failed=1, ai_failed=1, run_id=kwargs.get("run_id")),
    )
    monkeypatch.setattr(
        run_job,
        "run_intel_export_from_settings",
        lambda **kwargs: IntelExportResult(0, 0, "items", "digest", "pending"),
    )

    result = run_job.run_intel_once_from_settings(settings=_settings(db_path))

    assert result.status == "completed_with_errors"
    with create_session_factory(engine)() as session:
        row = session.scalar(select(IntelRun))
        assert row.status == "completed_with_errors"
        assert row.failed == 1


def test_export_surfaces_hotspot_with_failed_project_summary_as_pending(tmp_path):
    db_path = tmp_path / "export-ai-failed.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        source = Source(
            id="github_test",
            name="GitHub",
            transport="github",
            url="https://github.com/trending?since=daily",
            content_class="project_tool",
        )
        item = IntelItem(
            source=source,
            external_id="github_repo:owner/project",
            canonical_url="https://github.com/owner/project",
            title="GitHub repo: owner/project",
            content_class="project_tool",
            content_hash="a" * 64,
            status="hotspot",
        )
        item.ai_review = AIItemReview(
            content_class="project_tool",
            status="ai_failed",
            error_message="summary unavailable",
        )
        session.add(item)
        session.commit()

    result = run_intel_export_job(session_factory=session_factory, output_dir=tmp_path / "out")

    assert result.exported == 1
    assert result.pending == 1
