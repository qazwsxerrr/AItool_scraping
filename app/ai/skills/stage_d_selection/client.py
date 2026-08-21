"""HTTP adapter for the Stage-D event-selection skill."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config.settings import Settings

from .models import StageDSelectionResponse
from .parser import strict_parse_stage_d_selection
from .prompts import build_stage_d_provider_payload, preflight_stage_d_selection_schema


MIN_STAGE_D_TIMEOUT_SECONDS = 120.0


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


class StageDSelectionProviderError(RuntimeError):
    """Sanitized provider failure retained in the stage attempt audit."""

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
    """Provider client for one ordered-subset selection call."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = MIN_STAGE_D_TIMEOUT_SECONDS,
        max_retries: int = 2,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = _normalize_api_style(api_style)
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = max(0, int(max_retries))
        self._http_client = http_client
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
            api_style=settings.ai_review_api_style,
            timeout_seconds=max(float(settings.ai_review_timeout_seconds), MIN_STAGE_D_TIMEOUT_SECONDS),
            max_retries=settings.request_retries,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def select(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        edition: Mapping[str, Any] | None = None,
        max_selected: int,
    ) -> StageDSelectionCallResult:
        if not self.is_configured:
            raise RuntimeError("Shared AI provider is not configured")
        preflight_stage_d_selection_schema()
        candidate_ids = [int(event["event_id"]) for event in events]
        payload = build_stage_d_provider_payload(
            events,
            edition=edition or {},
            model=self.model,
            api_style=self.api_style,
            max_selected=max_selected,
        )
        return self._call(
            payload=payload,
            candidate_event_ids=candidate_ids,
            max_selected=max_selected,
        )

    def _call(
        self,
        *,
        payload: dict[str, Any],
        candidate_event_ids: Sequence[int],
        max_selected: int,
    ) -> StageDSelectionCallResult:
        endpoint = self._endpoint_url()
        request_metadata = _request_metadata(
            endpoint,
            payload,
            model=self.model,
            api_style=self.api_style,
            event_count=len(candidate_event_ids),
            max_selected=max_selected,
        )
        self.last_request_metadata = request_metadata
        self.last_raw_response = None
        self.last_error_metadata = None
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post_once(endpoint, payload, request_metadata=request_metadata)
                try:
                    raw_payload = response.json()
                except (TypeError, ValueError) as exc:
                    raise StageDSelectionProviderError(
                        "Stage D API returned invalid JSON",
                        status_code=_response_status(response),
                        error_code="invalid_json",
                        raw_response=_safe_response_payload(response),
                        request_metadata=request_metadata,
                        cause=exc,
                    ) from exc
                try:
                    parsed = strict_parse_stage_d_selection(
                        raw_payload,
                        candidate_event_ids=candidate_event_ids,
                        max_selected=max_selected,
                    )
                except (TypeError, ValueError) as exc:
                    raise StageDSelectionProviderError(
                        f"Stage D response failed schema validation: {exc}",
                        status_code=_response_status(response),
                        error_code="schema_validation_failed",
                        raw_response=raw_payload,
                        request_metadata=request_metadata,
                        cause=exc,
                    ) from exc
                self.last_raw_response = raw_payload
                completed_metadata = dict(request_metadata)
                completed_metadata["provider_attempts"] = attempt + 1
                return StageDSelectionCallResult(
                    parsed=parsed,
                    raw_response=raw_payload,
                    request_metadata=completed_metadata,
                )
            except BaseException as exc:
                last_error = exc
                if not _provider_failure_is_retryable(exc) or attempt >= self.max_retries:
                    if isinstance(exc, StageDSelectionProviderError):
                        self.last_raw_response = exc.raw_response
                        self.last_error_metadata = exc.audit_payload()
                        raise
                    error = StageDSelectionProviderError(
                        str(exc),
                        error_code="provider_error",
                        request_metadata=request_metadata,
                        cause=exc,
                    )
                    self.last_error_metadata = error.audit_payload()
                    raise error from exc
        raise StageDSelectionProviderError(
            str(last_error or "Stage D provider request failed"),
            error_code="provider_error",
            request_metadata=request_metadata,
            cause=last_error,
        )

    def _post_once(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        request_metadata: Mapping[str, Any] | None = None,
    ):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            if self._http_client is not None:
                try:
                    response = self._http_client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self.timeout_seconds,
                    )
                except TypeError:
                    response = self._http_client.post(url, headers=headers, json=payload)
            else:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                    http2=True,
                    trust_env=True,
                ) as client:
                    response = client.post(url, headers=headers, json=payload)
        except BaseException as exc:
            raise StageDSelectionProviderError(
                str(exc),
                error_code="transport_error",
                request_metadata=request_metadata,
                cause=exc,
            ) from exc
        status_code = _response_status(response)
        if status_code is not None and status_code >= 400:
            raise _provider_error_from_response(response, request_metadata=request_metadata, cause=None)
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            try:
                raise_for_status()
            except BaseException as exc:
                if _response_status(response) is not None and _response_status(response) >= 400:
                    raise _provider_error_from_response(
                        response,
                        request_metadata=request_metadata,
                        cause=exc,
                    ) from exc
                raise
        return response

    def _endpoint_url(self) -> str:
        if self.api_url is None:  # pragma: no cover - guarded by is_configured.
            raise RuntimeError("Shared AI provider is not configured")
        if self.api_style == "openai_chat" and not self.api_url.casefold().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.casefold().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url


def _normalize_api_style(value: Any) -> str:
    style = str(value or "generic_json").strip().casefold().replace("-", "_")
    style = {
        "responses": "openai_responses",
        "openai_response": "openai_responses",
        "chat": "openai_chat",
        "chat_completions": "openai_chat",
    }.get(style, style)
    if style not in {"generic_json", "openai_chat", "openai_responses"}:
        raise ValueError("api_style must be generic_json, openai_chat, or openai_responses")
    return style


def _provider_failure_is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, StageDSelectionProviderError):
        return exc.retryable
    name = exc.__class__.__name__.casefold()
    text = str(exc).casefold()
    return any(token in name or token in text for token in ("timeout", "connect", "network", "transport"))


def _request_metadata(
    endpoint: str,
    payload: Mapping[str, Any],
    *,
    model: str | None,
    api_style: str,
    event_count: int,
    max_selected: int,
) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return {
        "endpoint": _safe_url(endpoint),
        "api_style": api_style,
        "model": model,
        "event_count": int(event_count),
        "max_selected": int(max_selected),
        "phase": "selection",
        "prompt_version": "stage_d_selection_v1",
        "schema_version": "stage_d_selection_v1",
        "request_bytes": len(serialized),
        "request_sha256": digest,
        "request_hash": digest,
    }


def _provider_error_from_response(
    response: Any,
    *,
    request_metadata: Mapping[str, Any] | None,
    cause: BaseException | None,
) -> StageDSelectionProviderError:
    status_code = _response_status(response)
    body = _safe_response_payload(response)
    body_value = body.get("body") if isinstance(body, Mapping) else body
    error_code, error_message = _extract_error_fields(body_value, cause=cause)
    message = (
        error_message
        or (str(cause).strip() if cause is not None else "")
        or f"Stage D provider returned HTTP {status_code}"
    )
    return StageDSelectionProviderError(
        message,
        status_code=status_code,
        error_code=error_code or (f"http_{status_code}" if status_code is not None else None),
        raw_response=body,
        request_metadata=request_metadata,
        cause=cause,
    )


def _response_status(response: Any) -> int | None:
    try:
        value = getattr(response, "status_code", None)
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_response_payload(response: Any) -> dict[str, Any]:
    status_code = _response_status(response)
    body: Any = None
    response_json = getattr(response, "json", None)
    if callable(response_json):
        try:
            body = response_json()
        except (TypeError, ValueError, json.JSONDecodeError):
            body = None
    if body is None:
        try:
            body = getattr(response, "text", None)
        except BaseException:
            body = None
    headers: dict[str, str] = {}
    raw_headers = getattr(response, "headers", None)
    if raw_headers is not None:
        for key in ("content-type", "request-id", "x-request-id"):
            try:
                value = raw_headers.get(key)
            except AttributeError:
                value = None
            if value:
                headers[key] = _safe_text(value, 256)
    return {
        "status_code": status_code,
        "headers": headers,
        "body": _sanitize_value(body),
    }


def _extract_error_fields(
    value: Any,
    *,
    cause: BaseException | None,
) -> tuple[str | None, str | None]:
    if isinstance(value, Mapping):
        nested = value.get("error")
        nested = nested if isinstance(nested, Mapping) else {}
        code = nested.get("code") or value.get("error_code") or value.get("code") or value.get("type")
        message = nested.get("message") or value.get("error_message") or value.get("message")
        return (
            _safe_text(code, 256) if code else None,
            _safe_text(message, 2000) if message else None,
        )
    if isinstance(value, str) and value.strip():
        return None, _safe_text(value, 2000)
    return None, _safe_text(cause, 2000) if cause is not None else None


def _sanitize_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "<truncated>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).casefold()
            if any(
                token in normalized
                for token in ("authorization", "api_key", "token", "secret", "cookie", "password")
            ):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = _sanitize_value(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(child, depth=depth + 1) for child in list(value)[:64]]
    if isinstance(value, str):
        return _safe_text(value, 4000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value, 4000)


def _safe_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_url(value: str) -> str:
    try:
        parts = urlsplit(str(value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return _safe_text(value, 512)


__all__ = [
    "MIN_STAGE_D_TIMEOUT_SECONDS",
    "StageDSelectionCallResult",
    "StageDSelectionClient",
    "StageDSelectionProviderError",
    "SupportsPost",
]
