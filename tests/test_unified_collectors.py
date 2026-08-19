from __future__ import annotations

import httpx
import pytest

from app.collectors.feed import FeedCollector, ProductHuntCollector
from app.collectors.github import GitHubCollector, GitHubTrendingCollector
from app.collectors.router import CollectorRouter
from app.domain.models import SourceSpec


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        raise AssertionError("Product Hunt and feed collectors must not issue POST requests")


def _response(url, body, status=200, headers=None):
    return httpx.Response(
        status,
        request=httpx.Request("GET", url),
        headers=headers,
        content=body,
    )


def _feed_source(source_id="rss", *, transport="feed", feed=None, **kwargs):
    return SourceSpec(
        id=source_id,
        name=kwargs.pop("name", source_id),
        transport=transport,
        url=kwargs.pop("url", "https://example.test/feed.xml"),
        feed=feed,
        **kwargs,
    )


def _github_source(source_id="github", *, mode="search", url=None, **kwargs):
    options = {"mode": mode, **kwargs.pop("github", {})}
    return SourceSpec(
        id=source_id,
        name=kwargs.pop("name", source_id),
        transport="github",
        url=url or ("https://api.github.com/search/repositories" if mode == "search" else "https://github.com/trending?since=weekly"),
        github=options,
        **kwargs,
    )


def test_rss_collector_returns_fetch_batch_and_uses_shared_client():
    url = "https://example.test/feed.xml"
    body = b"<rss version='2.0'><channel><title>feed</title><item><title>AI release</title><link>https://example.test/a</link><description>model release</description></item></channel></rss>"
    client = _Client([_response(url, body)])
    source = _feed_source(url=url)

    batch = FeedCollector(client, sleeper=lambda _: None).collect(source, 10)

    assert batch.status == "success"
    assert batch.items_fetched == 1
    assert batch.items[0].source_id == "rss"
    assert client.calls[0][0:2] == ("GET", url)


def test_rsshub_uses_the_same_feed_implementation():
    url = "https://rsshub.example.test/openai"
    body = b"<rss version='2.0'><channel><item><title>RSSHub item</title></item></channel></rss>"
    client = _Client([_response(url, body)])
    source = _feed_source("rsshub", transport="rsshub", url=url)

    batch = FeedCollector(client, sleeper=lambda _: None).collect(source, 10)

    assert batch.status == "success"
    assert batch.items[0].title == "RSSHub item"


def test_reddit_feed_uses_standard_registry_url_without_query_rewrite():
    url = "https://www.reddit.com/r/LocalLLaMA/new/.rss"
    source = _feed_source(
        "reddit_local_llama_new",
        url=url,
        source_group="reddit_local_llama",
    )
    body = b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Reddit item</title></entry></feed>"
    client = _Client([_response(url, body)])

    batch = FeedCollector(client, retries=0, sleeper=lambda _: None).collect(source, 10)

    assert batch.status == "success"
    assert client.calls[0][1] == url
    assert "raw_json" not in client.calls[0][1]


def test_rss_503_retries_once_and_records_retry_count():
    url = "https://example.test/feed.xml"
    body = b"<rss version='2.0'><channel><item><title>retry</title></item></channel></rss>"
    client = _Client([_response(url, b"down", status=503), _response(url, body)])

    batch = FeedCollector(client, retries=3, sleeper=lambda _: None).collect(_feed_source(url=url), 10)

    assert batch.status == "success"
    assert batch.retry_count == 1
    assert len(client.calls) == 2


def test_rss_rate_limit_reset_header_controls_retry_sleep():
    url = "https://www.reddit.com/r/LocalLLaMA/new/.rss"
    body = b"<rss version='2.0'><channel><item><title>retry</title></item></channel></rss>"
    client = _Client(
        [
            _response(url, b"limited", status=429, headers={"x-ratelimit-reset": "7"}),
            _response(url, body),
        ]
    )
    sleeps = []

    batch = FeedCollector(client, retries=1, sleeper=sleeps.append).collect(
        _feed_source(url=url), 10
    )

    assert batch.status == "success"
    assert batch.retry_count == 1
    assert sleeps == [7.0]


def test_github_trending_html_maps_weekly_metrics_and_rank():
    url = "https://github.com/trending?since=weekly"
    body = b"""
    <html><body>
      <article class='Box-row'>
        <h2><a href='/owner/tool'>owner/tool</a></h2>
        <p>An AI tool</p>
        <span itemprop='programmingLanguage'>Python</span>
        <a href='/owner/tool/stargazers'>1,200</a>
        <a href='/owner/tool/forks'>80</a>
        <span class='float-sm-right'>250 stars this week</span>
      </article>
    </body></html>
    """
    client = _Client([_response(url, body)])
    source = _github_source(
        "github_trending_weekly_native",
        mode="trending",
        url=url,
        github={"period": "weekly"},
        content_class="project_tool",
    )

    batch = GitHubTrendingCollector(client, sleeper=lambda _: None).collect(source, 10)

    assert batch.status == "success"
    assert batch.transport == "github_trending_html"
    item = batch.items[0]
    assert item.metrics["stars"] == 1200
    assert item.metrics["forks"] == 80
    assert item.metrics["stars_since"] == 250
    assert item.metrics["trending_period"] == "weekly"
    assert item.metrics["trending_rank"] == 1
    assert item.metrics["language"] == "Python"


def test_github_search_adds_recent_push_filter_and_maps_metrics():
    url = "https://api.github.com/search/repositories"
    payload = {
        "items": [
            {
                "id": 42,
                "full_name": "owner/project",
                "html_url": "https://github.com/owner/project",
                "description": "AI tool",
                "stargazers_count": 1200,
                "forks_count": 33,
                "pushed_at": "2026-08-04T00:00:00Z",
                "owner": {"login": "owner"},
                "license": {"spdx_id": "MIT"},
            }
        ]
    }
    response = _response(url, b"{}")
    response.json = lambda: payload
    client = _Client([response])
    source = _github_source(
        mode="search",
        url=url,
        github={"query": "AI stars:>100", "sort": "stars", "order": "desc", "pushed_days": 30},
    )

    batch = GitHubCollector(client, base_url="https://api.github.com").collect(source, 10)

    assert batch.status == "success"
    assert batch.items[0].metrics["stars"] == 1200
    assert "pushed:>" in client.calls[0][2]["params"]["q"]


def test_github_search_rejects_payload_without_items_and_keeps_response_bytes():
    url = "https://api.github.com/search/repositories"
    response = _response(url, b'{"message":"rate limit metadata"}')
    response.json = lambda: {"message": "rate limit metadata"}
    client = _Client([response])
    source = _github_source(mode="search", url=url, github={"query": "AI"})

    batch = GitHubCollector(client, retries=0).collect(source, 10)

    assert batch.status == "failed"
    assert batch.error_code == "invalid_payload"
    assert batch.response_bytes == len(response.content)


def test_github_releases_maps_release_items():
    url = "https://api.github.com/repos/owner/project/releases"
    payload = [{
        "id": 7,
        "tag_name": "v1.2.0",
        "name": "Stable release",
        "body": "Fixes",
        "html_url": "https://github.com/owner/project/releases/tag/v1.2.0",
        "published_at": "2026-08-04T00:00:00Z",
        "author": {"login": "owner"},
    }]
    response = _response(url, b"[]")
    response.json = lambda: payload
    client = _Client([response])
    source = _github_source("github_releases", mode="releases", url=url)

    batch = GitHubCollector(client, retries=0).collect(source, 10)

    assert batch.status == "success"
    assert batch.items[0].kind == "github_release"
    assert batch.items[0].external_id == "github_release:7"
    assert "owner/project" in batch.items[0].title


def test_producthunt_atom_maps_engagement_and_github_link_without_post():
    url = "https://www.producthunt.com/feed"
    body = b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Tool</title><link href='https://example.test/tool'/><summary>HTTPS://WWW.GITHUB.COM/Owner/Repo.git</summary><published>2026-08-04T00:00:00Z</published></entry></feed>"
    client = _Client([_response(url, body)])
    source = _feed_source(
        "producthunt_feed",
        url=url,
        feed={"format": "atom", "adapter": "producthunt"},
        source_group="producthunt",
    )

    batch = ProductHuntCollector(client, sleeper=lambda _: None).collect(source, 10)

    assert batch.status == "success"
    assert batch.items[0].metrics["github_url"] == "https://github.com/Owner/Repo"
    assert batch.items[0].metrics["canonical_project_key"] == "Owner/Repo"
    assert batch.items[0].external_id == "github_repo:owner/repo"
    assert [method for method, _, _ in client.calls] == ["GET"]


def test_producthunt_extracts_namespaced_vote_and_comment_fields():
    url = "https://www.producthunt.com/feed"
    body = (
        b"<feed xmlns='http://www.w3.org/2005/Atom' xmlns:ph='https://producthunt.com/ns'>"
        b"<entry><title>Tool</title><link href='https://example.test/tool'/>"
        b"<ph:vote_count>123</ph:vote_count><ph:comments_count>7</ph:comments_count>"
        b"<published>2026-08-04T00:00:00Z</published></entry></feed>"
    )
    client = _Client([_response(url, body)])
    source = _feed_source(
        "producthunt_feed",
        url=url,
        feed={"format": "atom", "adapter": "producthunt"},
        source_group="producthunt",
    )

    batch = ProductHuntCollector(client, sleeper=lambda _: None).collect(source, 10)

    assert batch.items[0].metrics["votes"] == 123
    assert batch.items[0].metrics["comments"] == 7


def test_router_uses_explicit_nested_routes_and_rejects_unknown_transport():
    feed = object()
    direct_feed = object()
    rsshub = object()
    router = CollectorRouter(
        feed=feed,
        direct_feed=direct_feed,
        rsshub=rsshub,
        github=object(),
        github_trending=object(),
        producthunt=object(),
    )
    assert router.collector_for(_feed_source()) is feed
    assert router.collector_for(_feed_source(bypass_proxy=True)) is direct_feed
    assert router.collector_for(_feed_source(transport="rsshub")) is rsshub
    with pytest.raises(ValueError, match="unsupported source transport"):
        invalid = SourceSpec.model_construct(
            id="invalid",
            name="Invalid",
            transport="unknown",
            url="https://example.test/unknown",
            feed=None,
            github=None,
        )
        router.collector_for(invalid)
