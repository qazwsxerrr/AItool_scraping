from datetime import datetime, timezone

from app.config.source_registry import SourceConfig, load_source_registry
from app.jobs.fetch_job import run_fetch_job
from app.parsers.feed_parser import ParsedFeedItem
from app.storage.db import create_engine_from_url, create_session_factory, init_db


class CountingCollector:
    def __init__(self):
        self.calls = []

    def collect(self, source, limit=None):
        self.calls.append((source.id, limit))
        return [
            ParsedFeedItem(
                source_id=source.id,
                external_id=f"{source.id}-{index}",
                title=f"{source.id} item {index}",
                link=f"https://example.com/{source.id}/{index}",
                author=None,
                published_at=datetime(2026, 4, 22, 10, index, tzinfo=timezone.utc),
                raw_summary="summary",
                raw_content="content",
                raw_payload={"index": index},
                content_hash=f"{source.id}-hash-{index}",
            )
            for index in range(limit or 1)
        ]


def make_source(source_id, group, subtype, default_limit):
    return SourceConfig(
        id=source_id,
        name=source_id,
        type="rss",
        url=f"https://example.com/{source_id}.rss",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
        source_group=group,
        source_subtype=subtype,
        default_limit=default_limit,
    )


def test_fetch_job_filters_by_source_group_and_uses_source_default_limit(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'group.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    collector = CountingCollector()

    result = run_fetch_job(
        session_factory=session_factory,
        sources=[
            make_source("linux_do_top", "linux_do", "fixed_top", 15),
            make_source("reddit_new", "reddit_local_llama", "fixed_new", 40),
        ],
        collector=collector,
        source_group_filter="reddit_local_llama",
    )

    assert list(result.stats) == ["reddit_new"]
    assert collector.calls == [("reddit_new", 40)]
    assert result.total_fetched == 40


def test_default_registry_contains_main_site_metadata_and_x_is_env_gated():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    assert source_by_id["linux_do_top"].source_group == "linux_do"
    assert source_by_id["linux_do_top"].source_subtype == "fixed_top"
    assert source_by_id["linux_do_hot"].source_subtype == "fixed_hot"

    assert source_by_id["reddit_local_llama_new"].source_group == "reddit_local_llama"
    assert source_by_id["reddit_local_llama_new"].source_subtype == "fixed_new"
    assert source_by_id["reddit_local_llama_new"].default_limit == 40
    assert source_by_id["reddit_local_llama_search_agent"].source_subtype == "search"
    assert source_by_id["reddit_local_llama_search_agent"].search_query == "agent"

    skipped_ids = {item.source_id for item in result.skipped}
    assert "x_account_openai" in skipped_ids
    assert "x_search_github_launch" in skipped_ids
