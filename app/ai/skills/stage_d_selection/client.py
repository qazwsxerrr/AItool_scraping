"""Responses-only adapter for the Stage-D event-selection skill."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from app.ai.responses import ResponsesClient, ResponsesProviderError, SupportsPost
from app.config.settings import Settings

from .models import StageDSelectionResponse
from .parser import strict_parse_stage_d_selection
from .prompts import (
    STAGE_D_SELECTION_PROMPT_VERSION,
    build_stage_d_provider_payload,
    preflight_stage_d_selection_schema,
)


MIN_STAGE_D_TIMEOUT_SECONDS = 120.0


class StageDSelectionProviderError(RuntimeError):
    """Sanitized provider failure retained in the Stage-D task audit."""

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
        self.error_message = str(message or "Stage D provider request failed")
        self.raw_response = raw_response
        self.request_metadata = dict(request_metadata or {})
        self.cause = cause
        super().__init__(self.error_message)

    @property
    def retryable(self) -> bool:
        if self.status_code is not None:
            return int(self.status_code) == 429 or int(self.status_code) >= 500
        return str(self.error_code or "") in {"transport_error", "provider_error"}

    def audit_payload(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "raw_response": self.raw_response,
            "request_metadata": self.request_metadata,
        }


@dataclass(frozen=True)
class StageDSelectionCallResult:
    parsed: StageDSelectionResponse
    raw_response: Any
    request_metadata: Mapping[str, Any]


class StageDSelectionClient:
    """One strict JSON selection call through the shared Responses client."""

    transport = "responses"

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        timeout_seconds: float = MIN_STAGE_D_TIMEOUT_SECONDS,
        max_retries: int = 2,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self._responses = ResponsesClient(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_seconds=self.timeout_seconds,
            http_client=http_client,
        )
        self.last_raw_response: Any | None = None
        self.last_request_metadata: dict[str, Any] | None = None
        self.last_error_metadata: dict[str, Any] | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http_client: SupportsPost | None = None,
    ) -> "StageDSelectionClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            timeout_seconds=max(float(settings.ai_review_timeout_seconds), MIN_STAGE_D_TIMEOUT_SECONDS),
            max_retries=settings.request_retries,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return self._responses.is_configured

    def select(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        edition: Mapping[str, Any] | None = None,
        max_selected: int,
    ) -> StageDSelectionCallResult:
        if not self.is_configured:
            raise RuntimeError("Responses API is not configured")
        preflight_stage_d_selection_schema()
        candidate_ids = [int(event["event_id"]) for event in events]
        payload = build_stage_d_provider_payload(
            events,
            edition=edition or {},
            model=self.model,
            max_selected=max_selected,
        )
        request_metadata = _request_metadata(
            self._responses.endpoint_url,
            payload,
            model=self.model,
            event_count=len(candidate_ids),
            max_selected=max_selected,
        )
        self.last_request_metadata = request_metadata
        self.last_raw_response = None
        self.last_error_metadata = None
        last_error: StageDSelectionProviderError | None = None
        for attempt in range(self.max_retries + 1):
            raw_payload: Any | None = None
            try:
                raw_payload = self._responses.create(payload)
                parsed = strict_parse_stage_d_selection(
                    raw_payload,
                    candidate_event_ids=candidate_ids,
                    max_selected=max_selected,
                )
                completed = dict(request_metadata)
                completed["provider_attempts"] = attempt + 1
                self.last_raw_response = raw_payload
                return StageDSelectionCallResult(
                    parsed=parsed,
                    raw_response=raw_payload,
                    request_metadata=completed,
                )
            except StageDSelectionProviderError as exc:
                last_error = exc
            except ResponsesProviderError as exc:
                last_error = StageDSelectionProviderError(
                    str(exc),
                    status_code=exc.status_code,
                    error_code=exc.error_code or "provider_error",
                    raw_response=exc.raw_response,
                    request_metadata=request_metadata,
                    cause=exc,
                )
            except (TypeError, ValueError) as exc:
                last_error = StageDSelectionProviderError(
                    f"Stage D response failed schema validation: {exc}",
                    error_code="schema_validation_failed",
                    raw_response=raw_payload,
                    request_metadata=request_metadata,
                    cause=exc,
                )
            except BaseException as exc:
                last_error = StageDSelectionProviderError(
                    str(exc),
                    error_code="provider_error",
                    request_metadata=request_metadata,
                    cause=exc,
                )
            if not last_error.retryable or attempt >= self.max_retries:
                self.last_raw_response = last_error.raw_response
                self.last_error_metadata = last_error.audit_payload()
                raise last_error
        raise last_error or StageDSelectionProviderError(
            "Stage D provider request failed",
            error_code="provider_error",
            request_metadata=request_metadata,
        )


def _request_metadata(
    endpoint: str,
    payload: Mapping[str, Any],
    *,
    model: str | None,
    event_count: int,
    max_selected: int,
) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return {
        "endpoint": _safe_url(endpoint),
        "transport": "responses",
        "model": model,
        "event_count": int(event_count),
        "max_selected": int(max_selected),
        "phase": "selection",
        "prompt_version": STAGE_D_SELECTION_PROMPT_VERSION,
        "schema_version": "stage_d_selection_v1",
        "request_bytes": len(serialized),
        "request_sha256": digest,
        "request_hash": digest,
    }


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(str(value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return str(value or "").strip()[:512]


__all__ = [
    "MIN_STAGE_D_TIMEOUT_SECONDS",
    "StageDSelectionCallResult",
    "StageDSelectionClient",
    "StageDSelectionProviderError",
    "SupportsPost",
]
