from datetime import datetime, timezone

from app.config.source_registry import SourceConfig
from app.jobs.normalize_job import run_normalize_job
from app.parsers.feed_parser import ParsedFeedItem
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import NormalizedItem, RawItem
from app.storage.repository import RawItemRepository, SourceRepository


def source():
    return SourceConfig(
        id="source_a",
        name="Source A",
        type="rss",
        url="https://example.com/feed.xml",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
    )


def parsed_item(external_id, link, content_hash):
    return ParsedFeedItem(
        source_id="source_a",
        external_id=external_id,
        title="Tool launch",
        link=link,
        author="Ada",
        published_at=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
        raw_summary="<p>summary</p>",
        raw_content="<p>content</p>",
        raw_payload={"id": external_id},
        content_hash=content_hash,
    )


def seed_raw_items(session):
    SourceRepository(session).upsert_source(source())
    raw_repo = RawItemRepository(session)
    raw_repo.insert_if_new(parsed_item("guid-1", "https://example.com/tool?utm_source=a", "hash-1"))
    raw_repo.insert_if_new(parsed_item("guid-2", "https://example.com/tool?utm_medium=b", "hash-2"))
    session.commit()


def test_normalize_job_creates_normalized_items_and_marks_duplicates(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'job.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        seed_raw_items(session)

    first_run = run_normalize_job(session_factory=session_factory, limit=10)
    second_run = run_normalize_job(session_factory=session_factory, limit=10)

    assert first_run.processed == 2
    assert first_run.inserted == 1
    assert first_run.skipped == 1
    assert first_run.failed == 0
    assert second_run.processed == 0

    with session_factory() as session:
        assert session.query(NormalizedItem).count() == 1
        statuses = {item.external_id: item.status for item in session.query(RawItem).all()}
        assert statuses == {"guid-1": "normalized", "guid-2": "duplicate"}
