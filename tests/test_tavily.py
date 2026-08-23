from __future__ import annotations

import httpx
import pytest

from app.ai.tavily import TavilySearchClient, TavilySearchError


class _HttpClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


def _response(status_code: int, payload: dict) -> httpx.Response:
    request = httpx.Request("POST", "https://api.tavily.com/search")
    return httpx.Response(status_code, json=payload, request=request)


def test_tavily_search_returns_stable_auditable_sources_without_domain_filter():
    http = _HttpClient(
        _response(
            200,
            {
                "request_id": "request-1",
                "response_time": 0.2,
                "usage": {"credits": 1},
                "results": [
                    {
                        "title": "Official update",
                        "url": "https://vendor.example/news/update?ref=search",
                        "content": "The vendor announced the update.",
                        "score": 0.93,
                        "published_date": "2026-08-21",
                    }
                ],
            },
        )
    )
    client = TavilySearchClient(api_key="secret", http_client=http)

    first = client.search("vendor update", topic="news", max_results=5)
    second = client.search("vendor update", topic="news", max_results=5)

    assert first.results[0].result_id == second.results[0].result_id
    assert first.results[0].url == "https://vendor.example/news/update?ref=search"
    assert http.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert "include_domains" not in http.calls[0]["json"]
    assert "exclude_domains" not in http.calls[0]["json"]


def test_tavily_search_sanitizes_retryable_provider_errors():
    http = _HttpClient(_response(429, {"error": "rate limited"}))
    client = TavilySearchClient(api_key="secret", http_client=http)

    with pytest.raises(TavilySearchError, match="rate limited") as captured:
        client.search("query")

    assert captured.value.status_code == 429
    assert captured.value.retryable is True
    assert "secret" not in str(captured.value)
    assert len(http.calls) == 3
