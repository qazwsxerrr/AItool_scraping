from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from app.config.settings import Settings
from app.ai.skills.stage_d_selection import STAGE_D_SELECTION_SCHEMA_VERSION
from app.jobs import pipeline_orchestrator as orchestrator
from app.jobs.event_cluster_job import _load_published_daily_history
from app.jobs.export_job import IntelExportResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.draft_workspace import (
    audit_database_path,
    audit_database_url,
    create_daily_draft,
    daily_audit_exists,
    daily_draft_exists,
    draft_database_url,
)
from app.storage.models import DailyEdition, DailyEditionReportEntry, IntelEvent, IntelRun, Source
from app.storage.read_repository import UIReadRepository
from app.storage.repository import IntelRepository


def _source(session) -> None:
    session.add(
        Source(
            id="daily-source",
            name="Daily source",
            transport="feed",
            url="https://daily.example",
            source_group="official_blog",
            content_class="official_model_company",
        )
    )
    session.flush()


def _public_report(repo: IntelRepository, *, edition_date: str, title: str, key: str = "url:https://daily.example/old") -> None:
    repo.replace_published_daily_report(
        edition_date=edition_date,
        records=(
            {
                "event_key": key,
                "title": title,
                "original_title": title,
                "url": key.removeprefix("url:"),
                "source_ids": ["daily-source"],
                "source_refs": [{"source_id": "daily-source", "source_name": "Daily source", "is_primary": True}],
            },
        ),
    )


def _complete_draft(settings: Settings, edition_date: str) -> tuple[Settings, int]:
    workspace_settings = create_daily_draft(settings, edition_date)
    engine = create_engine_from_url(workspace_settings.database_url)
    factory = create_session_factory(engine)
    with factory() as session:
        repo = IntelRepository(session)
        _source(session)
        _, build = repo.start_daily_build(edition_date=edition_date)
        for stage_name in ("fetch", "screen", "analyze", "cluster", "stage_d"):
            repo.finish_stage(repo.ensure_stage(build.id, stage_name), status="succeeded")
        session.commit()
        return workspace_settings, int(build.id)


def test_draft_creation_does_not_modify_the_published_database(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'daily.db'}")
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    with create_session_factory(public_engine)() as session:
        _source(session)
        _public_report(IntelRepository(session), edition_date="2026-08-19", title="Morning report")
        session.commit()

    workspace_settings, run_id = _complete_draft(settings, "2026-08-19")

    with create_session_factory(public_engine)() as session:
        edition = session.scalar(select(DailyEdition).where(DailyEdition.edition_date == datetime(2026, 8, 19).date()))
        assert edition is not None
        assert edition.status == "published"
        assert [entry.title for entry in edition.report_entries] == ["Morning report"]
        assert session.get(IntelRun, run_id) is None

    assert workspace_settings.database_url == draft_database_url(settings.database_url, "2026-08-19")
    assert daily_draft_exists(settings, "2026-08-19")
    assert not daily_audit_exists(settings, "2026-08-19")
    assert audit_database_path(settings.database_url, "2026-08-19").parent == (
        tmp_path / "editions" / "2026-08-19"
    )


def test_stage_c_history_is_seeded_from_prior_published_reports_only(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'daily.db'}")
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    with create_session_factory(public_engine)() as session:
        _public_report(
            IntelRepository(session),
            edition_date="2026-08-18",
            title="Prior final event",
            key="url:https://daily.example/repeat",
        )
        session.commit()

    workspace_settings = create_daily_draft(settings, "2026-08-19")
    draft_engine = create_engine_from_url(workspace_settings.database_url)
    with create_session_factory(draft_engine)() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(edition_date="2026-08-19")
        current = repo.upsert_event(
            run_id=build.id,
            event_key="url:https://daily.example/repeat",
            title="Current event",
            canonical_url="https://daily.example/repeat",
        )
        session.commit()

    with create_session_factory(draft_engine)() as session:
        run = session.get(IntelRun, build.id)
        assert run is not None
        history = _load_published_daily_history(IntelRepository(session), run=run, days=3)
        assert [row["event_key"] for row in history] == ["url:https://daily.example/repeat"]
        current_event = session.get(IntelEvent, current.id)
        assert current_event is not None


def test_approved_draft_replaces_public_report_and_retains_its_full_audit(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'daily.db'}")
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    output_dir = tmp_path / "intel"
    final_dir = tmp_path / "daily" / "2026-08-19"
    final_dir.mkdir(parents=True)
    (final_dir / "intel_digest.md").write_text("old digest", encoding="utf-8")
    with create_session_factory(public_engine)() as session:
        _source(session)
        _public_report(IntelRepository(session), edition_date="2026-08-19", title="Morning report")
        session.commit()

    _, run_id = _complete_draft(settings, "2026-08-19")

    def fake_export(**kwargs):
        staging = Path(kwargs["artifact_dir"])
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "intel_digest.md").write_text("afternoon digest", encoding="utf-8")
        (staging / "intel_items.jsonl").write_text('{"title":"Afternoon report"}\n', encoding="utf-8")
        (staging / "manifest.json").write_text('{"edition_date":"2026-08-19"}\n', encoding="utf-8")
        return IntelExportResult(
            1,
            str(staging / "intel_items.jsonl"),
            str(staging / "intel_digest.md"),
            manifest_path=str(staging / "manifest.json"),
            records=(
                {
                    "event_key": "url:https://daily.example/afternoon",
                    "title": "Afternoon report",
                    "url": "https://daily.example/afternoon",
                    "source_ids": ["daily-source"],
                },
            ),
        )

    monkeypatch.setattr(orchestrator, "run_intel_export_from_settings", fake_export)
    result = orchestrator.publish_daily_draft_from_settings(
        settings=settings,
        edition_date="2026-08-19",
        output_dir=output_dir,
    )

    assert result.markdown_path == str(final_dir / "intel_digest.md")
    assert (final_dir / "intel_digest.md").read_text(encoding="utf-8") == "afternoon digest"
    assert not daily_draft_exists(settings, "2026-08-19")
    assert daily_audit_exists(settings, "2026-08-19")
    with create_session_factory(public_engine)() as session:
        edition = session.scalar(select(DailyEdition).where(DailyEdition.edition_date == datetime(2026, 8, 19).date()))
        assert edition is not None
        assert edition.status == "published"
        assert [entry.title for entry in edition.report_entries] == ["Afternoon report"]
        assert session.get(IntelRun, run_id) is None

    audit_engine = create_engine_from_url(audit_database_url(settings.database_url, "2026-08-19"))
    with create_session_factory(audit_engine)() as session:
        retained_run = session.get(IntelRun, run_id)
        assert retained_run is not None
        assert retained_run.status == "completed"
    status = orchestrator.pipeline_edition_status_from_settings(settings=settings, edition_date="2026-08-19")
    assert status.status == "published"
    assert status.draft_status is None
    assert status.audit_status == "retained"
    assert status.audit_path == str(audit_database_path(settings.database_url, "2026-08-19"))


def test_new_draft_keeps_the_prior_audit_until_its_own_publication(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'daily.db'}")
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    _, run_id = _complete_draft(settings, "2026-08-19")

    def fake_export(**kwargs):
        staging = Path(kwargs["artifact_dir"])
        staging.mkdir(parents=True, exist_ok=True)
        for name in ("intel_digest.md", "intel_items.jsonl", "manifest.json"):
            (staging / name).write_text(name, encoding="utf-8")
        return IntelExportResult(
            0,
            str(staging / "intel_items.jsonl"),
            str(staging / "intel_digest.md"),
            manifest_path=str(staging / "manifest.json"),
            records=(),
        )

    monkeypatch.setattr(orchestrator, "run_intel_export_from_settings", fake_export)
    orchestrator.publish_daily_draft_from_settings(
        settings=settings,
        edition_date="2026-08-19",
        output_dir=tmp_path / "intel",
    )
    audit_path = audit_database_path(settings.database_url, "2026-08-19")
    before = audit_path.read_bytes()

    new_workspace_settings = create_daily_draft(settings, "2026-08-19")

    assert daily_draft_exists(settings, "2026-08-19")
    assert daily_audit_exists(settings, "2026-08-19")
    assert audit_path.read_bytes() == before
    audit_engine = create_engine_from_url(audit_database_url(settings.database_url, "2026-08-19"))
    with create_session_factory(audit_engine)() as session:
        assert session.get(IntelRun, run_id) is not None
    draft_engine = create_engine_from_url(new_workspace_settings.database_url)
    with create_session_factory(draft_engine)() as session:
        assert IntelRepository(session).draft_run_for_edition("2026-08-19") is None


def test_publication_failure_restores_the_pending_draft_and_prior_audit(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'daily.db'}")
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    output_dir = tmp_path / "intel"

    def fake_export(**kwargs):
        staging = Path(kwargs["artifact_dir"])
        staging.mkdir(parents=True, exist_ok=True)
        for name in ("intel_digest.md", "intel_items.jsonl", "manifest.json"):
            (staging / name).write_text(name, encoding="utf-8")
        return IntelExportResult(
            1,
            str(staging / "intel_items.jsonl"),
            str(staging / "intel_digest.md"),
            manifest_path=str(staging / "manifest.json"),
            records=(
                {
                    "event_key": "url:https://daily.example/current",
                    "title": "Current report",
                    "url": "https://daily.example/current",
                },
            ),
        )

    monkeypatch.setattr(orchestrator, "run_intel_export_from_settings", fake_export)
    _complete_draft(settings, "2026-08-19")
    orchestrator.publish_daily_draft_from_settings(
        settings=settings,
        edition_date="2026-08-19",
        output_dir=output_dir,
    )
    audit_path = audit_database_path(settings.database_url, "2026-08-19")
    prior_audit = audit_path.read_bytes()

    _, next_run_id = _complete_draft(settings, "2026-08-19")

    def reject_public_replace(*args, **kwargs):
        raise RuntimeError("public write rejected")

    monkeypatch.setattr(IntelRepository, "replace_published_daily_report", reject_public_replace)
    with pytest.raises(RuntimeError, match="public write rejected"):
        orchestrator.publish_daily_draft_from_settings(
            settings=settings,
            edition_date="2026-08-19",
            output_dir=output_dir,
        )

    assert daily_draft_exists(settings, "2026-08-19")
    assert daily_audit_exists(settings, "2026-08-19")
    assert audit_path.read_bytes() == prior_audit
    with create_session_factory(create_engine_from_url(draft_database_url(settings.database_url, "2026-08-19")))() as session:
        assert session.get(IntelRun, next_run_id) is not None
    with create_session_factory(public_engine)() as session:
        assert [entry.title for entry in session.scalars(select(DailyEditionReportEntry)).all()] == ["Current report"]


def test_failed_or_partial_draft_leaves_the_published_report_visible(tmp_path, monkeypatch):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'daily.db'}")
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    output_dir = tmp_path / "intel"
    final_dir = tmp_path / "daily" / "2026-08-19"
    final_dir.mkdir(parents=True)
    (final_dir / "intel_digest.md").write_text("public digest", encoding="utf-8")
    with create_session_factory(public_engine)() as session:
        _source(session)
        _public_report(IntelRepository(session), edition_date="2026-08-19", title="Published report")
        session.commit()

    _complete_draft(settings, "2026-08-19")

    def failed_export(**kwargs):
        Path(kwargs["artifact_dir"]).mkdir(parents=True, exist_ok=True)
        raise RuntimeError("export broke")

    monkeypatch.setattr(orchestrator, "run_intel_export_from_settings", failed_export)
    with pytest.raises(RuntimeError, match="export broke"):
        orchestrator.publish_daily_draft_from_settings(
            settings=settings,
            edition_date="2026-08-19",
            output_dir=output_dir,
        )

    assert daily_draft_exists(settings, "2026-08-19")
    assert (final_dir / "intel_digest.md").read_text(encoding="utf-8") == "public digest"
    status = orchestrator.pipeline_edition_status_from_settings(settings=settings, edition_date="2026-08-19")
    assert status.status == "published"
    assert status.draft_status == "failed"
    with create_session_factory(public_engine)() as session:
        ui = UIReadRepository(session)
        edition = ui.resolve_edition(edition_date="2026-08-19")
        assert edition is not None
        assert [card.title for card in ui.list_featured_cards(edition=edition)] == ["Published report"]


def test_source_fetch_warning_does_not_block_publication(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'daily.db'}")
    public_engine = create_engine_from_url(settings.database_url)
    init_db(public_engine)
    with create_session_factory(public_engine)() as session:
        _public_report(IntelRepository(session), edition_date="2026-08-19", title="Published report")
        session.commit()

    workspace_settings = create_daily_draft(settings, "2026-08-19")
    draft_engine = create_engine_from_url(workspace_settings.database_url)
    with create_session_factory(draft_engine)() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(edition_date="2026-08-19")
        fetch_stage = repo.ensure_stage(build.id, "fetch")
        repo.finish_stage(
            fetch_stage,
            status="failed",
            metadata={
                "sources": 1,
                "fetched": 0,
                "inserted": 0,
                "failed": 1,
                "failed_sources": [{"source_id": "broken_source", "status": "failed"}],
            },
        )
        for stage_name in ("screen", "analyze", "cluster", "stage_d"):
            repo.finish_stage(repo.ensure_stage(build.id, stage_name), status="succeeded")
        build.partial = True
        build.partial_reason = "fetch_failed_sources:1"
        stage_d = repo.get_stage(build.id, "stage_d")
        assert stage_d is not None
        stage_d_task = repo.ensure_stage_task(
            stage_d,
            subject_type="run",
            subject_id=build.id,
            target_run_id=build.id,
        )
        repo.complete_stage_task(
            stage_d_task,
            result={
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "candidate_event_ids": [],
                "selected": [],
            },
        )
        session.commit()

    result = orchestrator.publish_daily_draft_from_settings(
        settings=settings,
        edition_date="2026-08-19",
        output_dir=tmp_path / "intel",
    )
    assert result.exported == 0
    assert not daily_draft_exists(settings, "2026-08-19")
    with create_session_factory(public_engine)() as session:
        assert list(session.scalars(select(DailyEditionReportEntry)).all()) == []
    manifest = (tmp_path / "daily" / "2026-08-19" / "manifest.json").read_text(encoding="utf-8")
    assert '"source_warnings"' in manifest
    assert "broken_source" in manifest


def test_init_db_does_not_run_an_automatic_historical_rebuild(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'existing.db'}"
    engine = create_engine_from_url(database_url)
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE intel_runs ADD COLUMN run_type TEXT NOT NULL DEFAULT 'daily'"))

    init_db(engine)

    assert "run_type" in {column["name"] for column in inspect(engine).get_columns("intel_runs")}
