from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.config.settings import Settings
from app.jobs import pipeline_orchestrator as orchestrator
from app.jobs.fetch_job import IntelFetchResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.draft_workspace import draft_database_url
from app.storage.models import IntelRun
from app.storage.repository import IntelRepository


def _session_factory(database_url: str = "sqlite:///:memory:"):
    engine = create_engine_from_url(database_url)
    init_db(engine)
    return create_session_factory(engine)


def test_explicit_edition_date_is_fixed_for_a_daily_build_after_midnight():
    session_factory = _session_factory()
    reference = datetime(2026, 8, 18, 16, 3, tzinfo=timezone.utc)

    with session_factory() as session:
        _, build = IntelRepository(session).start_daily_build(
            reference_time=reference,
            edition_date="2026-08-18",
        )
        session.commit()

    assert build.reference_time == reference
    assert build.edition_date == "2026-08-18"
    assert "edition_date" not in build.scope


def test_same_date_has_one_draft_and_a_new_start_discards_the_old_build():
    session_factory = _session_factory()
    with session_factory() as session:
        repo = IntelRepository(session)
        _, first = repo.start_daily_build(edition_date="2026-08-18")
        _, replacement = repo.start_daily_build(edition_date="2026-08-18")
        active = repo.draft_run_for_edition("2026-08-18")
        session.commit()
        session.expire_all()

        assert active is not None
        assert active.id == replacement.id
        assert session.scalar(select(func.count()).select_from(IntelRun)) == 1


def test_pipeline_start_resolves_only_the_date_draft(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")

    def fake_fetch(**kwargs):
        return IntelFetchResult(run_id=kwargs["run_id"])

    result = orchestrator.start_pipeline_run_from_settings(
        settings=settings,
        edition_date="2026-08-18",
        fetch_runner=fake_fetch,
    )

    assert result.edition_date == "2026-08-18"
    workspace_settings, resolved_run_id = orchestrator.resolve_pending_daily_draft_from_settings(
        settings=settings,
        edition_date="2026-08-18",
    )
    assert workspace_settings.database_url == draft_database_url(settings.database_url, "2026-08-18")
    assert resolved_run_id == result.run_id


def test_published_edition_has_no_pending_build_to_resume(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'published.db'}")
    session_factory = _session_factory(settings.database_url)
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.replace_published_daily_report(edition_date="2026-08-18", records=[])
        session.commit()

    with pytest.raises(ValueError, match="no pending draft"):
        orchestrator.resolve_pending_daily_draft_from_settings(
            settings=settings,
            edition_date="2026-08-18",
        )
