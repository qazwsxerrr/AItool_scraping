from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.jobs import run_job
from app.jobs.ai_review_job import AIReviewResult
from app.jobs.export_job import IntelExportResult
from app.jobs.fetch_job import IntelFetchResult
from app.jobs.pipeline_orchestrator import PipelineRunResult


def test_run_once_delegates_to_the_date_addressed_pipeline(tmp_path, monkeypatch):
    calls: dict[str, object] = {}

    def fake_pipeline(**kwargs):
        calls.update(kwargs)
        return PipelineRunResult(
            run_id=17,
            fetch=IntelFetchResult(run_id=17),
            ai_review=AIReviewResult(run_id=17),
            export=IntelExportResult(0, "items", "digest"),
            status="published",
        )

    monkeypatch.setattr(run_job, "run_pipeline_once_from_settings", fake_pipeline)

    result = run_job.run_intel_once_from_settings(
        settings=Settings(database_url=f"sqlite:///{tmp_path / 'run.db'}"),
        edition_date="2026-08-19",
        output_dir=str(tmp_path / "out"),
    )
    assert result.status == "published"
    assert calls["limit"] == 30
    assert calls["edition_date"] == "2026-08-19"
    assert calls["source"] is None
    assert calls["content_class"] is None


def test_run_once_rejects_ai_cap_that_would_make_a_daily_build_partial(tmp_path):
    with pytest.raises(ValueError, match="does not support --ai-limit"):
        run_job.run_intel_once_from_settings(
            settings=Settings(database_url=f"sqlite:///{tmp_path / 'cap.db'}"),
            ai_limit=1,
        )
