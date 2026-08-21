"""HTTP client for one bounded Stage-C aggregation request."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config.settings import Settings

from .models import StageCAggregationResponse, strict_parse_stage_c_aggregation
from .prompts import STAGE_C_PROMPT_VERSION, build_stage_c_provider_payload


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float): ...


class StageCAggregationProviderError(RuntimeError):
    """Sanitized provider failure retained in the Stage-C task audit."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        raw_response: Any | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.raw_response = raw_response
        self.request_metadata = dict(request_metadata or {})
        self.cause = cause
        super().__init__(str(message or "Stage C aggregation provider request failed"))

    @property
    def retryable(self) -> bool:
        if self.status_code is not None:
            return int(self.status_code) == 429 or int(self.status_code) >= 500
        if self.error_code in {"invalid_json", "schema_validation_failed", "configuration"}:
            return False
        return _looks_like_transport_failure(self.cause or self)


@dataclass(frozen=True)
class StageCAggregationCallResult:
    parsed: StageCAggregationResponse
    raw_response: Any
    request_metadata: Mapping[str, Any]


class StageCAggregationClient:
    """Call the configured provider for one already-bounded candidate batch."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None,
        api_style: str,
        timeout_seconds: float,
        max_retries: int = 2,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = str(api_style or "generic_json")
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "StageCAggregationClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            api_style=settings.ai_review_api_style,
            timeout_seconds=settings.ai_review_timeout_seconds,
            max_retries=settings.request_retries,
            http_client=http_client,
        )

    def aggregate(
        self,
        current_items: Sequence[Mapping[str, Any]],
        *,
        recent_history: Sequence[Mapping[str, Any]],
        edition: Mapping[str, Any],
    ) -> StageCAggregationCallResult:
        if not self.api_url or not self.api_key:
            raise RuntimeError("Stage C aggregation API is not configured")
        payload = build_stage_c_provider_payload(
            current_items,
            recent_history=recent_history,
            edition=edition,
            model=self.model,
            api_style=self.api_style,
        )
        endpoint = self._endpoint_url()
        request_metadata = _request_metadata(
            endpoint,
            payload,
            model=self.model,
            api_style=self.api_style,
            item_count=len(current_items),
            history_count=len(recent_history),
        )
        return self._call(
            endpoint=endpoint,
            payload=payload,
            request_metadata=request_metadata,
            item_ids=[int(item["id"]) for item in current_items],
            prior_event_keys=[str(row["event_key"]) for row in recent_history],
        )

    def _call(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        request_metadata: Mapping[str, Any],
        item_ids: Sequence[int],
        prior_event_keys: Sequence[str],
    ) -> StageCAggregationCallResult:
        last_error: StageCAggregationProviderError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post_once(endpoint, payload, request_metadata=request_metadata)
                try:
                    raw_response = response.json()
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise StageCAggregationProviderError(
                        "Stage C API returned invalid JSON",
                        status_code=_response_status(response),
                        error_code="invalid_json",
                        raw_response=_safe_response_payload(response),
                        request_metadata=request_metadata,
                        cause=exc,
                    ) from exc
                try:
                    parsed = strict_parse_stage_c_aggregation(
                        raw_response,
                        item_ids=item_ids,
                        prior_event_keys=prior_event_keys,
                    )
                except (TypeError, ValueError) as exc:
                    raise StageCAggregationProviderError(
                        f"Stage C response failed schema validation: {exc}",
                        status_code=_response_status(response),
                        error_code="schema_validation_failed",
                        raw_response=raw_response,
                        request_metadata=request_metadata,
                        cause=exc,
                    ) from exc
                completed_metadata = dict(request_metadata)
                completed_metadata["provider_attempts"] = attempt + 1
                return StageCAggregationCallResult(
                    parsed=parsed,
                    raw_response=raw_response,
                    request_metadata=completed_metadata,
                )
            except StageCAggregationProviderError as exc:
                last_error = exc
                completed_metadata = dict(exc.request_metadata or request_metadata)
                completed_metadata["provider_attempts"] = attempt + 1
                exc.request_metadata = completed_metadata
                if not exc.retryable or attempt >= self.max_retries:
                    raise
            except BaseException as exc:
                wrapped = StageCAggregationProviderError(
                    str(exc),
                    error_code="transport_error",
                    request_metadata=request_metadata,
                    cause=exc,
                )
                last_error = wrapped
                completed_metadata = dict(request_metadata)
                completed_metadata["provider_attempts"] = attempt + 1
                wrapped.request_metadata = completed_metadata
                if not wrapped.retryable or attempt >= self.max_retries:
                    raise wrapped from exc
        raise last_error or StageCAggregationProviderError(
            "Stage C aggregation provider request failed",
            error_code="provider_error",
            request_metadata=request_metadata,
        )

    def _post_once(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        request_metadata: Mapping[str, Any],
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            if self._http_client is not None:
                response = self._http_client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            else:
                # The local relay previously returned HTTP/2 stream errors for
                # the large v1 response. Retain HTTP/1.1 for this bounded v2
                # request contract as well.
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=False, trust_env=True) as client:
                    response = client.post(endpoint, headers=headers, json=payload)
        except BaseException as exc:
            raise StageCAggregationProviderError(
                str(exc),
                error_code="transport_error",
                request_metadata=request_metadata,
                cause=exc,
            ) from exc

        status_code = _response_status(response)
        if status_code is not None and status_code >= 400:
            raise StageCAggregationProviderError(
                f"Stage C provider returned HTTP {status_code}",
                status_code=status_code,
                error_code=f"http_{status_code}",
                raw_response=_safe_response_payload(response),
                request_metadata=request_metadata,
            )
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            try:
                raise_for_status()
            except BaseException as exc:
                raise StageCAggregationProviderError(
                    str(exc),
                    status_code=_response_status(response),
                    error_code="provider_error",
                    raw_response=_safe_response_payload(response),
                    request_metadata=request_metadata,
                    cause=exc,
                ) from exc
        return response

    def _endpoint_url(self) -> str:
        assert self.api_url is not None
        style = self.api_style.strip().casefold().replace("-", "_")
        if style == "openai_chat" and not self.api_url.casefold().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if style == "openai_responses" and not self.api_url.casefold().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url


def _request_metadata(
    endpoint: str,
    payload: Mapping[str, Any],
    *,
    model: str | None,
    api_style: str,
    item_count: int,
    history_count: int,
) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return {
        "endpoint": _safe_url(endpoint),
        "api_style": api_style,
        "model": model,
        "item_count": int(item_count),
        "history_count": int(history_count),
        "prompt_version": STAGE_C_PROMPT_VERSION,
        "request_bytes": len(serialized),
        "request_sha256": hashlib.sha256(serialized).hexdigest(),
    }


def _response_status(response: Any) -> int | None:
    try:
        value = getattr(response, "status_code", None)
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _looks_like_transport_failure(exc: BaseException) -> bool:
    name = exc.__class__.__name__.casefold()
    text = str(exc).casefold()
    return any(
        token in name or token in text
        for token in ("timeout", "timed out", "connect", "network", "transport", "stream", "internal_error")
    )


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _safe_response_payload(response: Any) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except BaseException:
        try:
            return str(response.text)[:4000]
        except BaseException:
            return None


__all__ = [
    "StageCAggregationCallResult",
    "StageCAggregationClient",
    "StageCAggregationProviderError",
]
