from __future__ import annotations

from datetime import datetime, timezone

from app.domain.models import SourceSpec
from app.jobs.source_health_job import run_source_health_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelRepository


def _source() -> SourceSpec:
    return SourceSpec(
        id="health_test",
        name="Health test",
        transport="feed",
        url="https://example.test/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        content_class="official_model_company",
        fetch_interval=60,
    )


def test_source_health_persists_failure_backoff_and_success_reset(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'health.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)

    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(_source(), policy=_source())
        repo.update_source_health(
            "health_test",
            success=False,
            error_code="rate_limited",
            error_message="HTTP 429",
            now=now,
        )
        session.commit()

    failed = run_source_health_job(session_factory=session_factory, source_filter="health_test")[0]
    assert failed.status == "failed"
    assert failed.consecutive_failures == 1
    assert failed.error_code == "rate_limited"
    assert failed.next_fetch_at is not None
    assert failed.next_fetch_at > now

    with session_factory() as session:
        IntelRepository(session).update_source_health(
            "health_test",
            success=True,
            etag='"abc"',
            last_modified="Wed, 12 Aug 2026 12:00:00 GMT",
            now=now,
        )
        session.commit()

    healthy = run_source_health_job(session_factory=session_factory, source_filter="health_test")[0]
    assert healthy.status == "healthy"
    assert healthy.consecutive_failures == 0
    assert healthy.error_code is None
    assert healthy.next_fetch_at is None
