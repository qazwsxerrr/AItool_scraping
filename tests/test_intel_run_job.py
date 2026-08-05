from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.jobs import run_job
from app.jobs.export_job import IntelExportResult
from app.jobs.fetch_job import IntelFetchResult
from app.jobs.process_job import IntelProcessResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelRun
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

    def fake_process(**kwargs):
        calls["process_source"] = kwargs.get("source_filter")
        return IntelProcessResult()

    def fake_export(**kwargs):
        return IntelExportResult(0, 0, "items", "digest", "pending")

    monkeypatch.setattr(run_job, "run_intel_fetch_from_settings", fake_fetch)
    monkeypatch.setattr(run_job, "run_intel_process_from_settings", fake_process)
    monkeypatch.setattr(run_job, "run_intel_export_from_settings", fake_export)

    result = run_job.run_intel_once_from_settings(
        settings=_settings(db_path),
        source="github_source",
        content_class="project_tool",
        limit=5,
    )
    assert result.status == "completed"
    assert result.run_id == calls["fetch_run_id"]
    assert calls["process_source"] == "github_source"
    with create_session_factory(engine)() as session:
        row = session.scalar(select(IntelRun))
        assert row.status == "completed"


def test_run_once_dry_run_does_not_create_database(tmp_path, monkeypatch):
    db_path = tmp_path / "dry.db"
    monkeypatch.setattr(
        run_job,
        "run_intel_fetch_from_settings",
        lambda **kwargs: IntelFetchResult(dry_run=True),
    )
    monkeypatch.setattr(
        run_job,
        "run_intel_process_from_settings",
        lambda **kwargs: IntelProcessResult(),
    )
    monkeypatch.setattr(
        run_job,
        "run_intel_export_from_settings",
        lambda **kwargs: IntelExportResult(0, 0, "items", "digest", "pending", dry_run=True),
    )
    result = run_job.run_intel_once_from_settings(settings=_settings(db_path), dry_run=True)
    assert result.status == "dry_run"
    assert not db_path.exists()
