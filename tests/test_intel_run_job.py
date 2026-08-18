from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.jobs import run_job
from app.jobs.ai_review_job import AIReviewResult
from app.jobs.event_cluster_job import EventClusterResult
from app.jobs.export_job import IntelExportResult
from app.jobs.fetch_job import IntelFetchResult, IntelSourceStats
from app.jobs.stage_d_job import StageDResult


def test_run_once_threads_one_run_id_and_default_fetch_cap(tmp_path, monkeypatch):
    calls: dict[str, object] = {}

    def fake_fetch(**kwargs):
        calls["run_id"] = kwargs.get("run_id")
        calls["limit"] = kwargs.get("limit_per_source")
        return IntelFetchResult(run_id=kwargs.get("run_id"), stats={"s": IntelSourceStats(source_id="s", fetched=1, inserted=1, status="success")})

    def fake_review(**kwargs):
        calls["review_run_id"] = kwargs.get("run_id")
        calls["ai_limit"] = kwargs.get("ai_limit")
        return AIReviewResult(run_id=kwargs.get("run_id"), candidate=0)

    monkeypatch.setattr(run_job, "run_intel_fetch_from_settings", fake_fetch)
    monkeypatch.setattr(run_job, "run_ai_review_from_settings", fake_review)
    monkeypatch.setattr(run_job, "run_event_cluster_from_settings", lambda **kwargs: EventClusterResult())
    monkeypatch.setattr(run_job, "run_stage_d_from_settings", lambda **kwargs: StageDResult())
    monkeypatch.setattr(run_job, "run_intel_export_from_settings", lambda **kwargs: IntelExportResult(0, "items", "digest"))

    result = run_job.run_intel_once_from_settings(settings=Settings(database_url=f"sqlite:///{tmp_path / 'run.db'}"), output_dir=str(tmp_path / "out"))
    assert result.status == "completed"
    assert calls["limit"] == 30
    assert calls["run_id"] == calls["review_run_id"] == result.run_id
    assert calls["ai_limit"] is None


def test_explicit_ai_cap_is_forwarded_for_partial_accounting(tmp_path, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(run_job, "run_intel_fetch_from_settings", lambda **kwargs: IntelFetchResult(run_id=kwargs.get("run_id"), stats={}))

    def fake_review(**kwargs):
        seen["ai_limit"] = kwargs.get("ai_limit")
        return AIReviewResult(run_id=kwargs.get("run_id"), partial=True, partial_reason="ai_limit:1")

    monkeypatch.setattr(run_job, "run_ai_review_from_settings", fake_review)
    monkeypatch.setattr(run_job, "run_event_cluster_from_settings", lambda **kwargs: EventClusterResult())
    monkeypatch.setattr(run_job, "run_stage_d_from_settings", lambda **kwargs: StageDResult())
    monkeypatch.setattr(run_job, "run_intel_export_from_settings", lambda **kwargs: IntelExportResult(0, "items", "digest"))
    result = run_job.run_intel_once_from_settings(settings=Settings(database_url=f"sqlite:///{tmp_path / 'cap.db'}"), ai_limit=1)
    assert result.status == "completed"
    assert seen["ai_limit"] == 1
