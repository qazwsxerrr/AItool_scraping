from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import Settings


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]): ...


@dataclass(frozen=True)
class TavilySearchResult:
    title: str | None
    url: str
    content: str | None
    retrieval_score: int
    raw_payload: dict[str, Any]

    @property
    def confidence(self) -> int:
        """Backward-compatible alias for retrieval relevance, not evidence confidence."""
        return self.retrieval_score


@dataclass(frozen=True)
class TavilySearchResponse:
    query: str
    request_id: str | None
    usage: dict[str, Any] | None
    results: list[TavilySearchResult]
    raw_response: dict[str, Any]


class TavilyClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.tavily.com",
        api_key: str | None,
        search_depth: str = "basic",
        max_results: int = 5,
        include_raw_content: bool = False,
        timeout_seconds: float = 20.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.search_depth = search_depth
        self.max_results = max_results
        self.include_raw_content = include_raw_content
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "TavilyClient":
        return cls(
            base_url=settings.tavily_base_url,
            api_key=settings.tavily_api_key,
            search_depth=settings.tavily_search_depth,
            max_results=settings.tavily_max_results,
            include_raw_content=settings.tavily_include_raw_content,
            timeout_seconds=settings.tavily_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str) -> TavilySearchResponse:
        if not self.is_configured:
            raise RuntimeError("Tavily API is not configured")

        payload = {
            "query": query,
            "search_depth": self.search_depth,
            "max_results": self.max_results,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": self.include_raw_content,
            "include_usage": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = self._endpoint_url()

        if self._http_client is not None:
            response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = client.post(url, headers=headers, json=payload)

        response.raise_for_status()
        data = response.json()
        return parse_tavily_response(data, fallback_query=query)

    def _endpoint_url(self) -> str:
        if self.base_url.endswith("/search"):
            return self.base_url
        return f"{self.base_url}/search"


def parse_tavily_response(data: dict[str, Any], *, fallback_query: str) -> TavilySearchResponse:
    raw_results = data.get("results") if isinstance(data.get("results"), list) else []
    results: list[TavilySearchResult] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            continue
        results.append(
            TavilySearchResult(
                title=_optional_str(raw.get("title")),
                url=url,
                content=_optional_str(raw.get("content")) or _optional_str(raw.get("raw_content")),
                retrieval_score=_score_to_confidence(raw.get("score")),
                raw_payload=raw,
            )
        )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    return TavilySearchResponse(
        query=str(data.get("query") or fallback_query),
        request_id=_optional_str(data.get("request_id")),
        usage=usage,
        results=results,
        raw_response=data,
    )


def _score_to_confidence(value: Any) -> int:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0
    if score <= 1:
        score *= 100
    return max(0, min(int(round(score)), 100))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
