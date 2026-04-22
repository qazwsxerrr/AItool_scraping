from datetime import datetime, timezone

from app.config.source_registry import SourceConfig
from app.parsers.feed_parser import ParsedFeedItem
from app.pipeline.normalize import normalize_raw_item
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import RawItem
from app.storage.repository import NormalizedItemRepository, RawItemRepository, SourceRepository


def make_source():
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


def make_parsed_item(**overrides):
    data = dict(
        source_id="source_a",
        external_id="guid-1",
        title="Tool launch",
        link="https://example.com/tool?utm_source=a",
        author="Ada",
        published_at=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
        raw_summary="summary",
        raw_content="content",
        raw_payload={"id": "guid-1"},
        content_hash="hash-1",
    )
    data.update(overrides)
    return ParsedFeedItem(**data)


def test_normalized_item_insert_is_idempotent_by_raw_item_and_dedupe_key(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'normalize.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        SourceRepository(session).upsert_source(make_source())
        raw_repo = RawItemRepository(session)
        first_raw_id = raw_repo.insert_if_new(make_parsed_item()).item_id
        second_raw_id = raw_repo.insert_if_new(
            make_parsed_item(
                external_id="guid-2",
                link="https://example.com/tool?utm_medium=b",
                content_hash="hash-2",
            )
        ).item_id
        session.commit()

    with session_factory() as session:
        normal_repo = NormalizedItemRepository(session)
        first_raw = session.get(RawItem, first_raw_id)
        second_raw = session.get(RawItem, second_raw_id)

        first = normal_repo.insert_if_new(normalize_raw_item(first_raw))
        same_raw = normal_repo.insert_if_new(normalize_raw_item(first_raw))
        duplicate_key = normal_repo.insert_if_new(normalize_raw_item(second_raw))
        session.commit()

    assert first.inserted is True
    assert same_raw.inserted is False
    assert same_raw.reason == "duplicate_raw_item"
    assert duplicate_key.inserted is False
    assert duplicate_key.reason == "duplicate_dedupe_key"
