"""HTTP client for the single Stage-C story aggregation call."""

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
    def __init__(
        self,
        message: str,
        *,
        raw_response: Any | None = None,
        request_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.raw_response = raw_response
        self.request_metadata = dict(request_metadata or {})
        super().__init__(message)


@dataclass(frozen=True)
class StageCAggregationCallResult:
    parsed: StageCAggregationResponse
    raw_response: Any
    request_metadata: Mapping[str, Any]


class StageCAggregationClient:
    """Call the configured provider once; any failure is returned as an error."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None,
        api_style: str,
        timeout_seconds: float,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = str(api_style or "generic_json")
        self.timeout_seconds = float(timeout_seconds)
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "StageCAggregationClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            api_style=settings.ai_review_api_style,
            timeout_seconds=settings.ai_review_timeout_seconds,
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
            item_count=len(current_items),
            history_count=len(recent_history),
        )
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
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=True, trust_env=True) as client:
                    response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            raw_response = response.json()
            parsed = strict_parse_stage_c_aggregation(
                raw_response,
                item_ids=[int(item["id"]) for item in current_items],
                prior_event_keys=[str(row["event_key"]) for row in recent_history],
            )
        except Exception as exc:
            raw_response = _safe_response_payload(locals().get("response"))
            raise StageCAggregationProviderError(
                str(exc),
                raw_response=raw_response,
                request_metadata=request_metadata,
            ) from exc
        return StageCAggregationCallResult(
            parsed=parsed,
            raw_response=raw_response,
            request_metadata=request_metadata,
        )

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
    item_count: int,
    history_count: int,
) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return {
        "endpoint": _safe_url(endpoint),
        "model": model,
        "item_count": int(item_count),
        "history_count": int(history_count),
        "prompt_version": STAGE_C_PROMPT_VERSION,
        "request_bytes": len(serialized),
        "request_sha256": hashlib.sha256(serialized).hexdigest(),
    }


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _safe_response_payload(response: Any) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        try:
            return str(response.text)[:4000]
        except Exception:
            return None


__all__ = [
    "StageCAggregationCallResult",
    "StageCAggregationClient",
    "StageCAggregationProviderError",
]
