"""Shared HTTP transport helpers for collectors.

The HTTP boundary is intentionally small: callers provide the client so the
same session can be shared by all sources and tests can inject a deterministic
fake client.  This module contains no source-specific routing or mapping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from app.config.settings import DEFAULT_USER_AGENT
from app.domain.models import FetchBatch, SourceSpec


class HTTPClient(Protocol):
    def get(self, url: str, **kwargs: Any): ...

    def post(self, url: str, **kwargs: Any): ...


@dataclass(frozen=True)
class RequestFailure:
    """Failure telemetry with tuple-style access for collector callers."""

    code: str
    message: str
    status: int | None
    response_bytes: int = 0

    def __iter__(self):
        yield self.code
        yield self.message
        yield self.status

    def __getitem__(self, index: int):
        return (self.code, self.message, self.status)[index]


def request_with_retry(
    client: HTTPClient,
    url: str,
    *,
    method: str = "get",
    json_body: dict[str, Any] | None = None,
    retries: int,
    user_agent: str = DEFAULT_USER_AGENT,
    max_response_bytes: int | None = None,
    extra_headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    sleeper=time.sleep,
) -> tuple[Any | None, int, RequestFailure | None]:
    """Execute one bounded HTTP request with retry and response telemetry."""

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/json, */*",
    }
    headers.update(extra_headers or {})
    last: RequestFailure | None = None
    last_attempt = 0
    for attempt in range(max(0, int(retries)) + 1):
        last_attempt = attempt
        try:
            request_kwargs: dict[str, Any] = {"headers": headers}
            if params is not None:
                request_kwargs["params"] = params
            if json_body is not None:
                request_kwargs["json"] = json_body
            if timeout_seconds is not None:
                request_kwargs["timeout"] = timeout_seconds
            requester = getattr(client, method.casefold(), None)
            if not callable(requester):
                raise TypeError(f"HTTP client does not support {method.upper()}")
            try:
                response = requester(url, **request_kwargs)
            except TypeError:
                # Tiny injected clients often expose only ``url`` and headers.
                request_kwargs.pop("timeout", None)
                response = requester(url, **request_kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            body = response_bytes(response)
            if max_response_bytes is not None and body > max_response_bytes:
                return None, attempt, RequestFailure(
                    "response_too_large",
                    f"response exceeds {max_response_bytes} bytes",
                    status,
                    body,
                )
            if status == 304:
                return response, attempt, None
            if 200 <= status < 300:
                return response, attempt, None
            last = RequestFailure(_http_error_code(status), f"HTTP {status}", status, body)
            if not _retryable(status, attempt, retries):
                break
            retry_after = _retry_after(response)
            sleeper(retry_after if retry_after is not None else min(0.5 * 2**attempt, 4.0))
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            last = RequestFailure(
                "timeout" if isinstance(exc, httpx.TimeoutException) else "request_error",
                str(exc),
                None,
                0,
            )
            if attempt >= retries:
                break
            sleeper(min(0.5 * 2**attempt, 4.0))
    return None, last_attempt, last or RequestFailure("request_error", "request failed", None, 0)


def failed_batch(source: SourceSpec, code: str, message: str, **kwargs: Any) -> FetchBatch:
    return FetchBatch(
        source=source,
        items=[],
        status="failed",
        error_code=code,
        error_message=str(message)[:4000],
        transport=kwargs.pop("transport", "httpx"),
        **kwargs,
    )


def absolute_url(url: str, base_url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return url if parsed.scheme and parsed.netloc else f"{base_url}/{url.lstrip('/')}"


def response_request_url(response: Any, fallback: str) -> str:
    try:
        request = getattr(response, "request", None)
        request_url = getattr(request, "url", None)
        if request_url:
            return str(request_url)
    except (AttributeError, RuntimeError):
        pass
    return fallback


def response_final_url(response: Any, fallback: str) -> str:
    try:
        value = getattr(response, "url", None)
        if value:
            return str(value)
    except (AttributeError, RuntimeError, ValueError):
        pass
    return fallback


def response_bytes(response: Any) -> int:
    try:
        return len(bytes(getattr(response, "content", b"") or b""))
    except (TypeError, ValueError, RuntimeError):
        return 0


def _http_error_code(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in {401, 403}:
        return "auth_or_access_denied"
    if status in {404, 410, 422}:
        return "permanent_http_error"
    if status in {408, 425, 500, 502, 503, 504}:
        return "transient_http_error"
    return "http_error"


def _retryable(status: int, attempt: int, retries: int) -> bool:
    if attempt >= retries:
        return False
    if status in {401, 403, 404, 410, 422}:
        return False
    if status in {429, 503}:
        return attempt < 1
    return status in {408, 425, 500, 502, 504}


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is not None:
        try:
            return min(max(float(raw), 0.0), 60.0)
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(str(raw))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return min(max((parsed - datetime.now(timezone.utc)).total_seconds(), 0.0), 60.0)
            except (TypeError, ValueError, OverflowError):
                pass

    # Reddit exposes a short, relative reset window as ``x-ratelimit-reset``
    # instead of ``Retry-After``.  Treat large values as Unix timestamps so
    # the same parser also works with providers that use an absolute reset.
    raw_reset = headers.get("x-ratelimit-reset") or headers.get("X-Ratelimit-Reset")
    try:
        reset = float(raw_reset)
    except (TypeError, ValueError):
        return None
    if reset > 10_000_000:
        reset -= time.time()
    return min(max(reset, 0.0), 60.0)


__all__ = [
    "HTTPClient",
    "RequestFailure",
    "absolute_url",
    "failed_batch",
    "request_with_retry",
    "response_bytes",
    "response_final_url",
    "response_request_url",
]
