from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, text

from app.jobs.event_cluster_job import _load_selected_daily_history_events
from app.jobs.export_job import IntelExportResult
from app.jobs import pipeline_orchestrator as orchestrator
from app.jobs.stage_d_job import _recent_daily_history
from app.config.settings import Settings
from app.storage.db import (
    _migrate_historical_daily_reports,
    create_engine_from_url,
    create_session_factory,
    init_db,
)
from app.storage.models import (
    AIItemReview,
    AIItemScreen,
    DailyEdition,
    DailyEditionReportEntry,
    FetchAttempt,
    IntelEvent,
    IntelEventItem,
    IntelEventStageDSnapshot,
    IntelItem,
    IntelRun,
    IntelRunItem,
    IntelRunStage,
    IntelRunStageAttempt,
    IntelRunStageTask,
    Source,
)
from app.storage.read_repository import UIReadRepository
from app.storage.repository import IntelRepository


def _session_factory():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


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


def _draft_event(repo: IntelRepository, run_id: int, *, external_id: str, title: str) -> IntelEvent:
    inserted = repo.insert_item(
        {
            "source_id": "daily-source",
            "external_id": external_id,
            "title": title,
            "canonical_url": f"https://daily.example/{external_id}",
            "content_class": "official_model_company",
        },
        run_id=run_id,
    )
    assert inserted.item_id is not None
    return repo.upsert_event(
        run_id=run_id,
        event_key=f"url:https://daily.example/{external_id}",
        title=title,
        canonical_url=f"https://daily.example/{external_id}",
        primary_item_id=inserted.item_id,
        source_ids=["daily-source"],
        source_group="official_blog",
    )


def _publish(repo: IntelRepository, run_id: int, event: IntelEvent) -> None:
    repo.publish_daily_report(
        run_id=run_id,
        records=[
            {
                "event_key": event.event_key,
                "title": event.title,
                "original_title": event.title,
                "summary_cn": event.summary_cn,
                "url": event.canonical_url,
                "display_score": 80,
                "topic": "model",
                "content_class": "official_model_company",
                "source_group": "official_blog",
                "source_ids": ["daily-source"],
                "source_refs": [
                    {
                        "source_id": "daily-source",
                        "source_name": "Daily source",
                        "source_url": event.canonical_url,
                        "title": event.title,
                        "is_primary": True,
                    }
                ],
            }
        ],
    )
    repo.delete_build(run_id)


def test_same_day_successful_rebuild_replaces_final_report_and_deletes_all_working_rows():
    session_factory = _session_factory()
    with session_factory() as session:
        repo = IntelRepository(session)
        _source(session)
        _, morning = repo.start_daily_build(edition_date="2026-08-19")
        morning_event = _draft_event(repo, morning.id, external_id="morning", title="Morning item")
        _publish(repo, morning.id, morning_event)
        session.commit()

    with session_factory() as session:
        repo = IntelRepository(session)
        _, afternoon = repo.start_daily_build(edition_date="2026-08-19")
        # The morning build was already removed. The current build begins with
        # no raw event/item state and can reuse the same external identity.
        assert session.scalar(select(func.count()).select_from(IntelItem)) == 0
        assert session.scalar(select(func.count()).select_from(IntelEvent)) == 0
        afternoon_event = _draft_event(repo, afternoon.id, external_id="morning", title="Afternoon replacement")
        _publish(repo, afternoon.id, afternoon_event)
        session.commit()

    with session_factory() as session:
        entries = list(session.scalars(select(DailyEditionReportEntry)).all())
        assert [entry.title for entry in entries] == ["Afternoon replacement"]
        for model in (
            IntelRun,
            IntelItem,
            IntelEvent,
            IntelEventItem,
            IntelEventStageDSnapshot,
            IntelRunItem,
            IntelRunStage,
            IntelRunStageTask,
            IntelRunStageAttempt,
            AIItemScreen,
            AIItemReview,
            FetchAttempt,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        edition = UIReadRepository(session).resolve_edition(edition_date="2026-08-19")
        assert edition is not None
        assert [card.title for card in UIReadRepository(session).list_featured_cards(edition=edition)] == [
            "Afternoon replacement"
        ]


def test_failed_draft_keeps_prior_published_report_until_a_later_success_replaces_it():
    session_factory = _session_factory()
    with session_factory() as session:
        repo = IntelRepository(session)
        _source(session)
        _, published = repo.start_daily_build(edition_date="2026-08-19")
        event = _draft_event(repo, published.id, external_id="clean", title="Clean report")
        _publish(repo, published.id, event)
        _, failed = repo.start_daily_build(edition_date="2026-08-19")
        _draft_event(repo, failed.id, external_id="polluted", title="Polluted draft")
        repo.mark_daily_build_failed(failed.id, error="source failure")
        session.commit()

    with session_factory() as session:
        edition = session.scalar(select(DailyEdition))
        assert edition is not None
        assert edition.status == "draft_failed"
        assert [entry.title for entry in edition.report_entries] == ["Clean report"]
        assert edition.draft_run_id is not None
        failed_run_id = int(edition.draft_run_id)

        repo = IntelRepository(session)
        _, replacement = repo.start_daily_build(edition_date="2026-08-19")
        # Starting over physically removes the failed draft and its pollution.
        assert session.scalar(select(func.count()).select_from(IntelItem)) == 0
        assert session.get(IntelRun, failed_run_id) is None or failed_run_id == replacement.id
        session.commit()


def test_daily_stage_history_reads_only_prior_final_report_entries():
    session_factory = _session_factory()
    with session_factory() as session:
        previous = DailyEdition(
            edition_date=datetime(2026, 8, 18, tzinfo=timezone.utc).date(),
            status="published",
            published_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )
        session.add(previous)
        session.flush()
        session.add(
            DailyEditionReportEntry(
                edition_id=previous.id,
                event_key="url:https://daily.example/repeat",
                display_order=1,
                title="Prior final event",
                original_title="Prior final event",
                url="https://daily.example/repeat",
                published_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
            )
        )
        repo = IntelRepository(session)
        _, current_run = repo.start_daily_build(edition_date="2026-08-19")
        current_event = repo.upsert_event(
            run_id=current_run.id,
            event_key="url:https://daily.example/repeat",
            title="Current event",
            canonical_url="https://daily.example/repeat",
        )
        session.commit()

    with session_factory() as session:
        run = session.get(IntelRun, current_run.id)
        assert run is not None
        history = _load_selected_daily_history_events(session, run=run, days=3)
        assert any(event.id < 0 and event.event_key == "url:https://daily.example/repeat" for event in history)
        current = session.get(IntelEvent, current_event.id)
        assert current is not None
        recent = _recent_daily_history(
            session,
            candidates=[{"event": current}],
            run=run,
            days=3,
        )
        assert recent == {
            current.id: {"appeared_recently": True, "prior_editions": ["2026-08-18"]}
        }


def test_daily_export_success_replaces_the_public_date_bundle_and_discards_the_draft(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'daily.db'}"
    settings = Settings(database_url=database_url)
    engine = create_engine_from_url(database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    output_dir = tmp_path / "intel"
    final_dir = tmp_path / "daily" / "2026-08-19"
    final_dir.mkdir(parents=True)
    (final_dir / "intel_digest.md").write_text("old digest", encoding="utf-8")
    (final_dir / "intel_items.jsonl").write_text('{"title":"old"}\n', encoding="utf-8")
    (final_dir / "manifest.json").write_text('{"old":true}\n', encoding="utf-8")

    with session_factory() as session:
        repo = IntelRepository(session)
        _source(session)
        _, old_run = repo.start_daily_build(edition_date="2026-08-19")
        old_event = _draft_event(repo, old_run.id, external_id="old", title="Old report")
        _publish(repo, old_run.id, old_event)
        _, draft = repo.start_daily_build(edition_date="2026-08-19")
        session.commit()
        draft_id = int(draft.id)

    def fake_export(**kwargs):
        staging = Path(kwargs["artifact_dir"])
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "intel_digest.md").write_text("new digest", encoding="utf-8")
        (staging / "intel_items.jsonl").write_text('{"title":"new"}\n', encoding="utf-8")
        (staging / "manifest.json").write_text('{"new":true}\n', encoding="utf-8")
        return IntelExportResult(
            1,
            str(staging / "intel_items.jsonl"),
            str(staging / "intel_digest.md"),
            manifest_path=str(staging / "manifest.json"),
            records=(
                {
                    "event_key": "url:https://daily.example/new",
                    "title": "New report",
                    "url": "https://daily.example/new",
                    "source_ids": ["daily-source"],
                },
            ),
        )

    monkeypatch.setattr(orchestrator, "run_intel_export_from_settings", fake_export)
    monkeypatch.setattr(orchestrator, "_sync_pipeline_run_status", lambda *args, **kwargs: "completed")
    result = orchestrator.run_pipeline_export_from_settings(
        settings=settings,
        run_id=draft_id,
        output_dir=output_dir,
    )

    assert result.markdown_path == str(final_dir / "intel_digest.md")
    assert (final_dir / "intel_digest.md").read_text(encoding="utf-8") == "new digest"
    with session_factory() as session:
        edition = session.scalar(select(DailyEdition))
        assert edition is not None
        assert [entry.title for entry in edition.report_entries] == ["New report"]
        assert session.get(IntelRun, draft_id) is None


def test_daily_export_failure_keeps_the_prior_bundle_and_report(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'daily.db'}"
    settings = Settings(database_url=database_url)
    engine = create_engine_from_url(database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    output_dir = tmp_path / "intel"
    final_dir = tmp_path / "daily" / "2026-08-19"
    final_dir.mkdir(parents=True)
    (final_dir / "intel_digest.md").write_text("old digest", encoding="utf-8")

    with session_factory() as session:
        repo = IntelRepository(session)
        _source(session)
        _, old_run = repo.start_daily_build(edition_date="2026-08-19")
        old_event = _draft_event(repo, old_run.id, external_id="old", title="Old report")
        _publish(repo, old_run.id, old_event)
        _, draft = repo.start_daily_build(edition_date="2026-08-19")
        session.commit()
        draft_id = int(draft.id)

    def failed_export(**kwargs):
        staging = Path(kwargs["artifact_dir"])
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "intel_digest.md").write_text("broken digest", encoding="utf-8")
        raise RuntimeError("export broke")

    monkeypatch.setattr(orchestrator, "run_intel_export_from_settings", failed_export)
    with pytest.raises(RuntimeError, match="export broke"):
        orchestrator.run_pipeline_export_from_settings(
            settings=settings,
            run_id=draft_id,
            output_dir=output_dir,
        )

    assert (final_dir / "intel_digest.md").read_text(encoding="utf-8") == "old digest"
    assert not list(final_dir.parent.glob(".2026-08-19.build-*"))
    with session_factory() as session:
        edition = session.scalar(select(DailyEdition))
        assert edition is not None
        assert edition.status == "draft_failed"
        assert [entry.title for entry in edition.report_entries] == ["Old report"]
        assert session.get(IntelRun, draft_id) is not None


def test_historical_report_import_keeps_only_final_entries_and_sources(tmp_path):
    """The one-time importer has no runtime fallback or retained build data."""

    database_url = f"sqlite:///{tmp_path / 'historical.db'}"
    engine = create_engine_from_url(database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        repo = IntelRepository(session)
        _source(session)
        _, build = repo.start_daily_build(edition_date="2026-08-18")
        event = _draft_event(repo, build.id, external_id="historical", title="Historical original")
        repo.upsert_event_stage_d_snapshot(
            event.id,
            run_id=build.id,
            display_order=1,
            display_score=88,
            selected=True,
            topic="model",
            source_group="official_blog",
            content_class="official_model_company",
            metadata={
                "display_title_zh": "历史最终标题",
                "run_id": build.id,
                "snapshot_key": "daily-2026-08-18",
                "nested": {"target_run_id": build.id, "visible": True},
            },
        )
        build.status = "completed"
        build.partial = False
        build.scope_json = '{"edition_date":"2026-08-18"}'
        build.finished_at = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
        session.flush()
        # ``snapshot_key`` exists only in the historical table layout. Add it
        # to this isolated fixture solely to exercise the one-time importer.
        session.execute(text("ALTER TABLE intel_event_stage_d_snapshots ADD COLUMN snapshot_key TEXT"))
        session.execute(
            text(
                "UPDATE intel_event_stage_d_snapshots "
                "SET snapshot_key = :snapshot_key WHERE run_id = :run_id"
            ),
            {"snapshot_key": "daily-2026-08-18", "run_id": build.id},
        )
        session.commit()

    _migrate_historical_daily_reports(engine)

    with session_factory() as session:
        edition = session.scalar(select(DailyEdition).where(DailyEdition.edition_date == datetime(2026, 8, 18).date()))
        assert edition is not None
        assert edition.status == "published"
        assert edition.draft_run_id is None
        assert [entry.title for entry in edition.report_entries] == ["历史最终标题"]
        assert edition.report_entries[0].metadata_dict == {
            "display_title_zh": "历史最终标题",
            "nested": {"visible": True},
        }
        assert session.scalar(select(func.count()).select_from(Source)) == 1
        for model in (
            IntelRun,
            IntelItem,
            IntelEvent,
            IntelEventItem,
            IntelEventStageDSnapshot,
            IntelRunItem,
            IntelRunStage,
            IntelRunStageTask,
            IntelRunStageAttempt,
            AIItemScreen,
            AIItemReview,
            FetchAttempt,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0

    assert "snapshot_key" not in {
        column["name"] for column in inspect(engine).get_columns("intel_event_stage_d_snapshots")
    }
