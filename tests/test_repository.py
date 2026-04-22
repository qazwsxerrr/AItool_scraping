from datetime import datetime, timezone

from app.config.source_registry import SourceConfig
from app.parsers.feed_parser import ParsedFeedItem
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import RawItemRepository, SourceRepository


def make_source(source_id="source_a"):
    return SourceConfig(
        id=source_id,
        name="Source A",
        type="rss",
        url="https://example.com/feed.xml",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
    )


def make_item(**overrides):
    data = dict(
        source_id="source_a",
        external_id="guid-1",
        title="Tool launch",
        link="https://example.com/tool",
        author="Ada",
        published_at=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
        raw_summary="summary",
        raw_content="content",
        raw_payload={"id": "guid-1"},
        content_hash="hash-1",
    )
    data.update(overrides)
    return ParsedFeedItem(**data)


def test_raw_item_insert_is_idempotent_by_external_id_link_and_hash(tmp_path):
    db_path = tmp_path / "items.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        SourceRepository(session).upsert_source(make_source())
        raw_repo = RawItemRepository(session)

        first = raw_repo.insert_if_new(make_item())
        second = raw_repo.insert_if_new(make_item(title="Different title same ids"))
        third = raw_repo.insert_if_new(
            make_item(external_id="guid-2", link="https://example.com/other", content_hash="hash-1")
        )
        session.commit()

    assert first.inserted is True
    assert second.inserted is False
    assert second.reason == "duplicate_external_id"
    assert third.inserted is False
    assert third.reason == "duplicate_content_hash"
