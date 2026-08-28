"""Structured-output adapter for the Stage-D event-selection skill."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from app.ai.structured import StructuredApiStyle, StructuredClient, StructuredProviderError, SupportsPost
from app.config.settings import Settings

from .models import StageDSelectionResponse
from .parser import strict_parse_stage_d_selection
from .prompts import (
    STAGE_D_SELECTION_JSON_SCHEMA,
    STAGE_D_SELECTION_PROMPT_VERSION,
    STAGE_D_SELECTION_TASK,
    build_stage_d_selection_request,
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
    """One strict JSON selection call through the shared structured client."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: StructuredApiStyle = "responses",
        timeout_seconds: float = MIN_STAGE_D_TIMEOUT_SECONDS,
        max_retries: int = 2,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self._structured = StructuredClient(
            api_style=api_style,
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
            api_style=settings.ai_structured_api_style,
            timeout_seconds=max(float(settings.ai_review_timeout_seconds), MIN_STAGE_D_TIMEOUT_SECONDS),
            max_retries=settings.request_retries,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return self._structured.is_configured

    @property
    def transport(self) -> StructuredApiStyle:
        return self._structured.transport

    def select(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        edition: Mapping[str, Any] | None = None,
        max_selected: int,
    ) -> StageDSelectionCallResult:
        if not self.is_configured:
            raise RuntimeError("Structured API is not configured")
        preflight_stage_d_selection_schema()
        candidate_ids = [int(event["event_id"]) for event in events]
        instructions, input_value = build_stage_d_selection_request(
            events,
            edition=edition or {},
            max_selected=max_selected,
        )
        request_metadata = _request_metadata(
            self._structured.endpoint_url,
            {"instructions": instructions, "input": input_value, "schema": "stage_d_selection_v1"},
            transport=self.transport,
            model=self.model,
            event_count=len(candidate_ids),
            max_selected=max_selected,
        )
        self.last_request_metadata = request_metadata
        self.last_raw_response = None
        self.last_error_metadata = None
        last_error: StageDSelectionProviderError | None = None
        repair_attempts = 0
        for attempt in range(self.max_retries + 1):
            raw_payload: Any | None = None
            structured_result = None
            schema_repair_scheduled = False
            try:
                structured_result = self._structured.structured(
                    instructions=instructions,
                    input_value=input_value,
                    schema_name=STAGE_D_SELECTION_TASK,
                    schema=STAGE_D_SELECTION_JSON_SCHEMA,
                )
                raw_payload = structured_result.raw_response
                parsed = strict_parse_stage_d_selection(
                    structured_result.data,
                    candidate_event_ids=candidate_ids,
                    max_selected=max_selected,
                )
                completed = dict(request_metadata)
                completed["provider_attempts"] = attempt + 1
                if repair_attempts:
                    completed["schema_repair_attempts"] = repair_attempts
                self.last_raw_response = raw_payload
                return StageDSelectionCallResult(
                    parsed=parsed,
                    raw_response=raw_payload,
                    request_metadata=completed,
                )
            except StageDSelectionProviderError as exc:
                last_error = exc
            except StructuredProviderError as exc:
                last_error = StageDSelectionProviderError(
                    str(exc),
                    status_code=exc.status_code,
                    error_code=exc.error_code or "provider_error",
                    raw_response=exc.raw_response,
                    request_metadata=request_metadata,
                    cause=exc,
                )
            except (TypeError, ValueError) as exc:
                repair_attempts += 1
                last_error = StageDSelectionProviderError(
                    f"Stage D response failed schema validation: {exc}",
                    error_code="schema_validation_failed",
                    raw_response=raw_payload,
                    request_metadata=request_metadata,
                    cause=exc,
                )
                if attempt < self.max_retries:
                    input_value = _schema_repair_input(
                        input_value,
                        candidate_event_ids=candidate_ids,
                        validation_error=str(exc),
                        invalid_response=structured_result.data if structured_result is not None else None,
                    )
                    schema_repair_scheduled = True
            except BaseException as exc:
                last_error = StageDSelectionProviderError(
                    str(exc),
                    error_code="provider_error",
                    request_metadata=request_metadata,
                    cause=exc,
                )
            if schema_repair_scheduled:
                continue
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
    transport: StructuredApiStyle,
    model: str | None,
    event_count: int,
    max_selected: int,
) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return {
        "endpoint": _safe_url(endpoint),
        "transport": transport,
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


def _schema_repair_input(
    input_value: Mapping[str, Any],
    *,
    candidate_event_ids: Sequence[int],
    validation_error: str,
    invalid_response: Any,
) -> dict[str, Any]:
    repaired = dict(input_value)
    repaired["validation_feedback"] = {
        "repair_instruction": (
            "上一轮 Stage D 输出未通过本地契约校验。请重新返回完整 JSON。"
            "每个 candidate_event_id 必须且只能出现在 selected 或 unselected 之一；"
            "不要返回候选池外 event_id；selected 数量不得超过 max_selected。"
        ),
        "validation_error": validation_error,
        "candidate_event_ids": [int(event_id) for event_id in candidate_event_ids],
        "previous_invalid_response": _compact_json_value(invalid_response, limit=12_000),
    }
    return repaired


def _compact_json_value(value: Any, *, limit: int) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


__all__ = [
    "MIN_STAGE_D_TIMEOUT_SECONDS",
    "StageDSelectionCallResult",
    "StageDSelectionClient",
    "StageDSelectionProviderError",
    "SupportsPost",
]
