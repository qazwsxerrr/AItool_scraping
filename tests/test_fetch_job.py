from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from app.collectors.rss_collector import HTTPFeedCollector
from app.config.source_registry import SourceConfig
from app.jobs.fetch_job import run_fetch_job
from app.parsers.feed_parser import ParsedFeedItem
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import RawItem


class FakeCollector:
    def collect(self, source, limit=None):
        if source.id == "bad_source":
            raise RuntimeError("network down")
        items = [
            ParsedFeedItem(
                source_id=source.id,
                external_id="guid-1",
                title="Good item",
                link="https://example.com/good",
                author=None,
                published_at=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
                raw_summary="summary",
                raw_content="content",
                raw_payload={"id": "guid-1"},
                content_hash=f"{source.id}-hash-1",
            )
        ]
        return items[:limit] if limit is not None else items


def source(source_id, url="https://example.com/feed.xml"):
    return SourceConfig(
        id=source_id,
        name=source_id,
        type="rss",
        url=url,
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
    )


def test_fetch_job_continues_when_one_source_fails(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'fetch.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    result = run_fetch_job(
        session_factory=session_factory,
        sources=[source("good_source"), source("bad_source")],
        collector=FakeCollector(),
        limit_per_source=5,
    )

    assert result.total_inserted == 1
    assert result.stats["good_source"].inserted == 1
    assert result.stats["bad_source"].failed == 1
    assert "network down" in result.stats["bad_source"].error

    with session_factory() as session:
        assert session.query(RawItem).count() == 1


def test_http_feed_collector_uses_curl_fallback_when_httpx_times_out(monkeypatch):
    rss_body = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <guid>linux-do-1</guid>
      <title>Linux DO AI Tool</title>
      <link>https://linux.do/t/topic/1</link>
    </item>
  </channel>
</rss>
"""

    class TimeoutClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def get(self, url):
            raise httpx.TimeoutException("timed out")

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=rss_body, stderr=b"")

    monkeypatch.setattr("app.collectors.rss_collector.httpx.Client", TimeoutClient)
    monkeypatch.setattr("app.collectors.rss_collector.subprocess.run", fake_run)

    collector = HTTPFeedCollector(timeout_seconds=1, retries=0)
    items = collector.collect(source("linux_do_top", "https://linux.do/top.rss"), limit=30)

    assert len(items) == 1
    assert items[0].title == "Linux DO AI Tool"
