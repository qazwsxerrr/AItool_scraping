from __future__ import annotations

import httpx

from app.collectors.unified import GitHubCollector, ProductHuntCollector, RSSCollector
from app.domain.models import SourceSpec


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _response(url, body, status=200, headers=None):
    return httpx.Response(
        status,
        request=httpx.Request("GET", url),
        headers=headers,
        content=body,
    )


def test_rss_collector_returns_fetch_batch_and_uses_shared_client():
    url = "https://example.test/feed.xml"
    body = b"<rss version='2.0'><channel><title>feed</title><item><title>AI release</title><link>https://example.test/a</link><description>model release</description></item></channel></rss>"
    client = _Client([_response(url, body)])
    source = SourceSpec(id="rss", name="RSS", type="rss", url=url)
    batch = RSSCollector(client, sleeper=lambda _: None).collect(source, 10)
    assert batch.status == "success"
    assert batch.items_fetched == 1
    assert batch.items[0].source_id == "rss"
    assert client.calls[0][0] == url


def test_github_trending_feed_maps_star_text_and_push_snapshot():
    url = "https://rsshub.example/github/trending/daily/python"
    body = b"<rss version='2.0'><channel><item><title>owner/tool</title><link>https://github.com/owner/tool</link><description>1.2k stars, 80 forks</description><pubDate>Tue, 04 Aug 2026 00:00:00 GMT</pubDate></item></channel></rss>"
    client = _Client([_response(url, body)])
    source = SourceSpec(
        id="github_trending_python_daily",
        name="GitHub Trending",
        type="rsshub",
        url=url,
        content_class="project_tool",
        collector_type="rsshub",
        selection_policy={"mode": "github_active_high_star", "pushed_days": 30, "min_stars": 100},
    )
    item = RSSCollector(client, sleeper=lambda _: None).collect(source, 10).items[0]
    assert item.metrics["stars"] == 1200
    assert item.metrics["forks"] == 80
    assert item.metrics["pushed_at"]


def test_rss_503_retries_once_and_records_retry_count():
    url = "https://example.test/feed.xml"
    body = b"<rss version='2.0'><channel><item><title>retry</title></item></channel></rss>"
    client = _Client([
        _response(url, b"down", status=503),
        _response(url, body),
    ])
    source = SourceSpec(id="rss", name="RSS", type="rss", url=url)
    batch = RSSCollector(client, retries=3, sleeper=lambda _: None).collect(source, 10)
    assert batch.status == "success"
    assert batch.retry_count == 1
    assert len(client.calls) == 2


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
    client = _Client([_response(url, b"{}")])
    # Replace response.json without relying on private httpx helpers.
    response = _response(url, b"{}")
    response.json = lambda: payload
    client.responses = [response]
    source = SourceSpec(
        id="github",
        name="GitHub",
        type="github_api",
        url=url,
        source_subtype="search_repositories",
        search_query="AI stars:>100",
        search_sort="stars",
        search_order="desc",
        search_pushed_days=30,
    )
    batch = GitHubCollector(client, base_url="https://api.github.com").collect(source, 10)
    assert batch.status == "success"
    assert batch.items[0].metrics["stars"] == 1200
    assert "pushed:>" in client.calls[0][1]["params"]["q"]


def test_github_search_rejects_payload_without_items_and_keeps_response_bytes():
    url = "https://api.github.com/search/repositories"
    response = _response(url, b'{"message":"rate limit metadata"}')
    response.json = lambda: {"message": "rate limit metadata"}
    client = _Client([response])
    source = SourceSpec(
        id="github",
        name="GitHub",
        type="github_api",
        url=url,
        source_subtype="search_repositories",
        search_query="AI",
    )

    batch = GitHubCollector(client, retries=0).collect(source, 10)

    assert batch.status == "failed"
    assert batch.error_code == "invalid_payload"
    assert batch.response_bytes == len(response.content)


def test_producthunt_extracts_votes_and_github_link():
    url = "https://www.producthunt.com/feed"
    body = b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Tool</title><link href='https://example.test/tool'/><summary>https://github.com/owner/tool</summary><published>2026-08-04T00:00:00Z</published></entry></feed>"
    client = _Client([_response(url, body)])
    source = SourceSpec(id="producthunt_feed", name="Product Hunt", type="atom", url=url, source_group="producthunt")
    batch = ProductHuntCollector(client, sleeper=lambda _: None).collect(source, 10)
    assert batch.status == "success"
    assert batch.items[0].metrics["github_url"] == "https://github.com/owner/tool"


def test_producthunt_extracts_namespaced_vote_and_comment_fields():
    url = "https://www.producthunt.com/feed"
    body = (
        b"<feed xmlns='http://www.w3.org/2005/Atom' xmlns:ph='https://producthunt.com/ns'>"
        b"<entry><title>Tool</title><link href='https://example.test/tool'/>"
        b"<ph:vote_count>123</ph:vote_count><ph:comments_count>7</ph:comments_count>"
        b"<published>2026-08-04T00:00:00Z</published></entry></feed>"
    )
    client = _Client([_response(url, body)])
    source = SourceSpec(id="producthunt_feed", name="Product Hunt", type="atom", url=url, source_group="producthunt")

    batch = ProductHuntCollector(client, sleeper=lambda _: None).collect(source, 10)

    assert batch.items[0].metrics["votes"] == 123
    assert batch.items[0].metrics["comments"] == 7


def test_producthunt_canonicalizes_www_and_mixed_case_github_links():
    url = "https://www.producthunt.com/feed"
    body = (
        b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Tool</title>"
        b"<link href='https://example.test/tool'/>"
        b"<summary>HTTPS://WWW.GITHUB.COM/Owner/Repo.git</summary>"
        b"<published>2026-08-04T00:00:00Z</published></entry></feed>"
    )
    client = _Client([_response(url, body)])
    source = SourceSpec(id="producthunt_feed", name="Product Hunt", type="atom", url=url, source_group="producthunt")

    batch = ProductHuntCollector(client, sleeper=lambda _: None).collect(source, 10)

    item = batch.items[0]
    assert item.metrics["github_url"] == "https://github.com/Owner/Repo"
    assert item.metrics["canonical_project_key"] == "Owner/Repo"
    assert item.external_id == "github_repo:owner/repo"


def test_producthunt_api_maps_votes_comments_and_rank_when_token_is_configured():
    api_url = "https://api.producthunt.com/v2/api/graphql"
    payload = {
        "data": {
            "posts": {
                "nodes": [
                    {
                        "id": "123",
                        "name": "API Tool",
                        "tagline": "An AI workflow",
                        "description": "Project at https://github.com/owner/tool",
                        "url": "https://www.producthunt.com/posts/api-tool",
                        "website": "https://example.test",
                        "votesCount": 456,
                        "commentsCount": 12,
                        "createdAt": "2026-08-04T00:00:00Z",
                        "featuredAt": "2026-08-04T01:00:00Z",
                        "dailyRank": 3,
                        "weeklyRank": 8,
                        "productLinks": [],
                    }
                ]
            }
        }
    }
    response = _response(api_url, b"{}")
    response.json = lambda: payload
    client = _Client([response])
    source = SourceSpec(
        id="producthunt_feed",
        name="Product Hunt",
        type="atom",
        url="https://www.producthunt.com/feed",
        source_group="producthunt",
    )

    batch = ProductHuntCollector(
        client,
        api_token="token",
        api_url=api_url,
        sleeper=lambda _: None,
    ).collect(source, 10)

    item = batch.items[0]
    assert batch.transport == "producthunt_api"
    assert item.metrics["votes"] == 456
    assert item.metrics["comments"] == 12
    assert item.metrics["daily_rank"] == 3
    assert item.metrics["github_url"] == "https://github.com/owner/tool"
    assert client.calls[0][1]["headers"]["Authorization"] == "Bearer token"
    assert client.calls[0][1]["json"]["variables"]["first"] == 10
