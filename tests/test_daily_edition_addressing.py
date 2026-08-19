from __future__ import annotations

from datetime import datetime, timezone

from app.config.settings import Settings
from app.jobs import pipeline_orchestrator as orchestrator
from app.jobs.fetch_job import IntelFetchResult
from app.jobs.stage_d_job import _recent_daily_history
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventStageDSnapshot
from app.storage.repository import IntelRepository


def _session_factory(database_url: str = "sqlite:///:memory:"):
    engine = create_engine_from_url(database_url)
    init_db(engine)
    return create_session_factory(engine)


def test_explicit_edition_date_survives_a_retry_after_midnight():
    session_factory = _session_factory()
    reference = datetime(2026, 8, 18, 16, 3, tzinfo=timezone.utc)

    with session_factory() as session:
        run = IntelRepository(session).start_run(
            reference_time=reference,
            edition_date="2026-08-18",
        )
        session.commit()

    assert run.reference_time == reference
    assert run.edition_date == "2026-08-18"
    assert run.scope["edition_date"] == "2026-08-18"


def test_date_resolver_uses_the_newest_internal_attempt():
    session_factory = _session_factory()
    with session_factory() as session:
        repo = IntelRepository(session)
        first = repo.start_run(
            reference_time=datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
            edition_date="2026-08-18",
        )
        second = repo.start_run(
            reference_time=datetime(2026, 8, 18, 16, tzinfo=timezone.utc),
            edition_date="2026-08-18",
        )
        session.commit()

        resolved = repo.latest_run_for_edition("2026-08-18")

    assert first.id != second.id
    assert resolved is not None
    assert resolved.id == second.id


def test_pipeline_start_persists_requested_edition_date(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")

    def fake_fetch(**kwargs):
        return IntelFetchResult(run_id=kwargs["run_id"])

    result = orchestrator.start_pipeline_run_from_settings(
        settings=settings,
        edition_date="2026-08-18",
        fetch_runner=fake_fetch,
    )

    assert result.edition_date == "2026-08-18"
    assert orchestrator.resolve_pipeline_run_id_from_settings(
        settings=settings,
        edition_date="2026-08-18",
    ) == result.run_id


def test_recent_daily_history_uses_the_date_addressed_partial_edition():
    session_factory = _session_factory()
    with session_factory() as session:
        repo = IntelRepository(session)
        previous = repo.start_run(
            reference_time=datetime(2026, 8, 18, 10, tzinfo=timezone.utc),
            edition_date="2026-08-18",
        )
        previous.status = "completed_with_errors"
        current = repo.start_run(
            reference_time=datetime(2026, 8, 18, 16, 3, tzinfo=timezone.utc),
            edition_date="2026-08-19",
        )
        event = IntelEvent(
            event_key="daily-history-event",
            title="Daily history event",
            first_seen_at=datetime.now(timezone.utc),
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                IntelEventStageDSnapshot(
                    snapshot_key="daily-2026-08-18",
                    event_id=event.id,
                    run_id=previous.id,
                    selected=True,
                ),
                IntelEventStageDSnapshot(
                    snapshot_key="debug-2026-08-18",
                    event_id=event.id,
                    run_id=previous.id,
                    selected=True,
                ),
            ]
        )
        session.commit()

        history = _recent_daily_history(
            session,
            candidates=[{"event": event}],
            run=current,
            days=3,
        )

    assert history == {
        event.id: {
            "appeared_recently": True,
            "prior_editions": ["2026-08-18"],
        }
    }
