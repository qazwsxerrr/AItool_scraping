from __future__ import annotations

import json

import pytest

from app.config.settings import Settings
from app.search.tavily_client import TavilyClient, TavilySearchResponse


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def test_tavily_client_posts_search_request_and_parses_results():
    settings = Settings(
        tavily_base_url="https://api.tavily.com",
        tavily_api_key="secret-key",
        tavily_search_depth="basic",
        tavily_max_results=3,
        tavily_include_raw_content=False,
    )
    http_client = FakeHttpClient(
        FakeResponse(
            {
                "query": "Example MCP github",
                "request_id": "req_123",
                "usage": {"credits": 1},
                "results": [
                    {
                        "title": "Example MCP",
                        "url": "https://github.com/example/mcp",
                        "content": "A real MCP server with install docs.",
                        "score": 0.91,
                        "raw_content": None,
                    }
                ],
            }
        )
    )
    client = TavilyClient.from_settings(settings, http_client=http_client)

    response = client.search("Example MCP github")

    assert isinstance(response, TavilySearchResponse)
    assert response.query == "Example MCP github"
    assert response.request_id == "req_123"
    assert response.usage == {"credits": 1}
    assert response.results[0].url == "https://github.com/example/mcp"
    assert response.results[0].confidence == 91

    call = http_client.calls[0]
    assert call["url"] == "https://api.tavily.com/search"
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert call["json"] == {
        "query": "Example MCP github",
        "search_depth": "basic",
        "max_results": 3,
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
        "include_usage": True,
    }


def test_tavily_client_requires_api_key():
    client = TavilyClient.from_settings(Settings(tavily_api_key=None))

    assert client.is_configured is False
    with pytest.raises(RuntimeError, match="Tavily API is not configured"):
        client.search("Example")
