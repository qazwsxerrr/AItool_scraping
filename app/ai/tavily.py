"""Small, auditable Tavily Search REST client used by Stage C and Stage D."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx


class SupportsPost(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...


class TavilySearchError(RuntimeError):
    """Sanitized Tavily failure suitable for stage/task audit records."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        raw_response: Any | None = None,
    ) -> None:
        self.status_code = status_code
        self.retryable = bool(retryable)
        self.raw_response = raw_response
        super().__init__(str(message or "Tavily search failed"))


@dataclass(frozen=True)
class TavilySearchResult:
    result_id: str
    title: str | None
    url: str
    content: str | None
    score: float | None
    published_date: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "score": self.score,
            "published_date": self.published_date,
        }


@dataclass(frozen=True)
class TavilySearchResponse:
    query: str
    request_id: str | None
    response_time: float | None
    results: tuple[TavilySearchResult, ...]
    usage: Mapping[str, Any]
    raw_response: Mapping[str, Any]
    provider_attempts: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "request_id": self.request_id,
            "response_time": self.response_time,
            "results": [row.as_dict() for row in self.results],
            "usage": dict(self.usage),
            "provider_attempts": self.provider_attempts,
        }


class TavilySearchClient:
    """Call Tavily directly so returned source URLs remain locally auditable."""

    def __init__(
        self,
        *,
        api_key: str | None,
        api_url: str = "https://api.tavily.com",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        http_client: SupportsPost | None = None,
    ) -> None:
        self._api_key = str(api_key or "").strip() or None
        self.api_url = str(api_url or "https://api.tavily.com").rstrip("/")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self._http_client = http_client

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self.api_url)

    @property
    def endpoint_url(self) -> str:
        if self.api_url.endswith("/search"):
            return self.api_url
        return self.api_url + "/search"

    def search(
        self,
        query: str,
        *,
        topic: str = "general",
        max_results: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> TavilySearchResponse:
        if not self.is_configured:
            raise TavilySearchError("Tavily API is not configured")
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise TavilySearchError("Tavily query is required")
        normalized_topic = str(topic or "general").strip().casefold()
        if normalized_topic not in {"general", "news"}:
            normalized_topic = "general"
        payload: dict[str, Any] = {
            "query": normalized_query,
            "topic": normalized_topic,
            "search_depth": "basic",
            "max_results": max(1, min(10, int(max_results))),
            "include_answer": False,
            "include_raw_content": False,
            "include_usage": True,
        }
        if start_date:
            payload["start_date"] = str(start_date)
        if end_date:
            payload["end_date"] = str(end_date)

        owns_client = self._http_client is None
        client: SupportsPost = self._http_client or httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            http2=True,
            trust_env=True,
        )
        response: Any | None = None
        raw: Any | None = None
        attempts = 0
        try:
            for attempt in range(self.max_retries + 1):
                attempts = attempt + 1
                response = None
                raw = None
                try:
                    response = client.post(
                        self.endpoint_url,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                        timeout=self.timeout_seconds,
                    )
                    raw = response.json()
                    response.raise_for_status()
                    break
                except Exception as exc:
                    status_code = getattr(response, "status_code", None)
                    error = TavilySearchError(
                        _error_message(raw) or str(exc) or "Tavily search failed",
                        status_code=int(status_code) if status_code is not None else None,
                        retryable=status_code is None or int(status_code) == 429 or int(status_code) >= 500,
                        raw_response=raw if isinstance(raw, Mapping) else None,
                    )
                    if not error.retryable or attempt >= self.max_retries:
                        raise error from exc
        finally:
            if owns_client and hasattr(client, "close"):
                client.close()

        if not isinstance(raw, Mapping):
            raise TavilySearchError("Tavily response must be a JSON object", raw_response=raw)
        results: list[TavilySearchResult] = []
        for index, value in enumerate(raw.get("results") or []):
            if not isinstance(value, Mapping):
                continue
            url = _public_url(value.get("url"))
            if not url:
                continue
            result_id = hashlib.sha256(
                json.dumps(
                    {
                        "request_id": raw.get("request_id"),
                        "query": normalized_query,
                        "url": url,
                        "index": index,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:24]
            results.append(
                TavilySearchResult(
                    result_id=result_id,
                    title=_text(value.get("title"), 500),
                    url=url,
                    content=_text(value.get("content"), 4_000),
                    score=_float(value.get("score")),
                    published_date=_text(value.get("published_date"), 64),
                )
            )
        return TavilySearchResponse(
            query=normalized_query,
            request_id=_text(raw.get("request_id"), 256),
            response_time=_float(raw.get("response_time")),
            results=tuple(results),
            usage=dict(raw.get("usage") or {}) if isinstance(raw.get("usage"), Mapping) else {},
            raw_response=dict(raw),
            provider_attempts=attempts,
        )


def _public_url(value: Any) -> str | None:
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if parts.scheme not in {"http", "https"} or not host or host in {"localhost", "localhost.localdomain"}:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return None
    return urlunsplit((parts.scheme.casefold(), parts.netloc, parts.path or "/", parts.query, ""))


def _error_message(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    error = value.get("error")
    if isinstance(error, Mapping):
        return _text(error.get("message") or error.get("detail"), 2_000)
    return _text(error or value.get("message") or value.get("detail"), 2_000)


def _text(value: Any, limit: int) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


__all__ = [
    "TavilySearchClient",
    "TavilySearchError",
    "TavilySearchResponse",
    "TavilySearchResult",
]
