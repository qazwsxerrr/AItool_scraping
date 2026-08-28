"""Provider-neutral structured-output entry point for Stages A, B, and D."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence, TypeAlias

import httpx

StructuredApiStyle: TypeAlias = Literal["responses", "chat_completions"]
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", flags=re.IGNORECASE | re.DOTALL)


class StructuredProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        raw_response: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.raw_response = raw_response
        self.retryable = status_code is None or status_code == 429 or (status_code is not None and status_code >= 500)


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


@dataclass(frozen=True)
class StructuredResult:
    data: Mapping[str, Any]
    raw_response: Mapping[str, Any]
    transport: StructuredApiStyle


class StructuredAdapter(Protocol):
    transport: StructuredApiStyle

    @property
    def is_configured(self) -> bool: ...

    @property
    def endpoint_url(self) -> str: ...

    def structured(
        self,
        *,
        instructions: str,
        input_value: Mapping[str, Any] | Sequence[Any] | str,
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> StructuredResult: ...


class _BaseStructuredAdapter:
    transport: StructuredApiStyle

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self._http_client = http_client

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key and self.model)

    def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("Structured API requires AI_REVIEW_API_URL, AI_REVIEW_API_KEY, and AI_REVIEW_MODEL")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            if self._http_client is not None:
                try:
                    response = self._http_client.post(
                        self.endpoint_url,
                        headers=headers,
                        json=dict(body),
                        timeout=self.timeout_seconds,
                    )
                except TypeError:
                    response = self._http_client.post(self.endpoint_url, headers=headers, json=dict(body))
            else:
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=True, trust_env=True) as client:
                    response = client.post(self.endpoint_url, headers=headers, json=dict(body))
        except httpx.HTTPError as exc:
            raise StructuredProviderError(str(exc), error_code=exc.__class__.__name__) from exc
        except Exception as exc:
            raise StructuredProviderError(str(exc), error_code=exc.__class__.__name__) from exc

        status_code = getattr(response, "status_code", None)
        try:
            data = response.json()
        except Exception as exc:
            raise StructuredProviderError(
                "Structured API returned invalid JSON",
                status_code=status_code,
                error_code="invalid_json",
            ) from exc
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            try:
                raise_for_status()
            except Exception as exc:
                raise StructuredProviderError(
                    _provider_error(data) or str(exc),
                    status_code=status_code,
                    error_code=_provider_error_code(data),
                    raw_response=data,
                ) from exc
        if not isinstance(data, Mapping):
            raise StructuredProviderError(
                "Structured API returned a non-object payload",
                status_code=status_code,
                raw_response=data,
            )
        return dict(data)


class ResponsesAdapter(_BaseStructuredAdapter):
    transport: StructuredApiStyle = "responses"

    @property
    def endpoint_url(self) -> str:
        return _structured_endpoint(self.api_url, self.transport)

    def structured(
        self,
        *,
        instructions: str,
        input_value: Mapping[str, Any] | Sequence[Any] | str,
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> StructuredResult:
        raw_response = self._post(
            {
                "model": self.model,
                "input": _messages(instructions, input_value),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": dict(schema),
                    }
                },
            }
        )
        return StructuredResult(
            data=_parse_json_object(_responses_text(raw_response), raw_response=raw_response),
            raw_response=raw_response,
            transport=self.transport,
        )


class ChatAdapter(_BaseStructuredAdapter):
    transport: StructuredApiStyle = "chat_completions"

    @property
    def endpoint_url(self) -> str:
        return _structured_endpoint(self.api_url, self.transport)

    def structured(
        self,
        *,
        instructions: str,
        input_value: Mapping[str, Any] | Sequence[Any] | str,
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> StructuredResult:
        raw_response = self._post(
            {
                "model": self.model,
                "messages": _messages(instructions, input_value),
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": dict(schema),
                    },
                },
            }
        )
        return StructuredResult(
            data=_parse_json_object(_chat_text(raw_response), raw_response=raw_response),
            raw_response=raw_response,
            transport=self.transport,
        )


class StructuredClient:
    """The sole structured-output business entry point for Stages A, B, and D."""

    def __init__(
        self,
        *,
        api_style: StructuredApiStyle,
        api_url: str | None,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        if api_style not in {"responses", "chat_completions"}:
            raise ValueError("api_style must be responses or chat_completions")
        adapter_type = ResponsesAdapter if api_style == "responses" else ChatAdapter
        self._adapter: StructuredAdapter = adapter_type(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )

    @property
    def transport(self) -> StructuredApiStyle:
        return self._adapter.transport

    @property
    def is_configured(self) -> bool:
        return self._adapter.is_configured

    @property
    def endpoint_url(self) -> str:
        return self._adapter.endpoint_url

    def structured(
        self,
        *,
        instructions: str,
        input_value: Mapping[str, Any] | Sequence[Any] | str,
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> StructuredResult:
        return self._adapter.structured(
            instructions=instructions,
            input_value=input_value,
            schema_name=schema_name,
            schema=schema,
        )


def _structured_endpoint(api_url: str | None, transport: StructuredApiStyle) -> str:
    if not api_url:
        raise RuntimeError("Structured API is not configured")
    if transport == "responses":
        if api_url.casefold().endswith("/responses"):
            return api_url
        if api_url.casefold().endswith("/chat/completions"):
            return api_url[: -len("/chat/completions")] + "/responses"
        return f"{api_url}/responses"
    if api_url.casefold().endswith("/chat/completions"):
        return api_url
    if api_url.casefold().endswith("/responses"):
        return api_url[: -len("/responses")] + "/chat/completions"
    return f"{api_url}/chat/completions"


def _messages(
    instructions: str,
    input_value: Mapping[str, Any] | Sequence[Any] | str,
) -> list[dict[str, str]]:
    content = input_value if isinstance(input_value, str) else json.dumps(input_value, ensure_ascii=False, default=str)
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": str(content)},
    ]


def _responses_text(response: Mapping[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if str(part.get("type") or "") not in {"output_text", "text"}:
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    raise StructuredProviderError("Responses API returned no output text", raw_response=dict(response))


def _chat_text(response: Mapping[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise StructuredProviderError("Chat Completions API returned no choices", raw_response=dict(response))
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise StructuredProviderError("Chat Completions API returned no assistant message", raw_response=dict(response))
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [part.get("text") for part in content if isinstance(part, Mapping) and isinstance(part.get("text"), str)]
        text = "".join(parts).strip()
        if text:
            return text
    raise StructuredProviderError("Chat Completions API returned no message content", raw_response=dict(response))


def _parse_json_object(text: str, *, raw_response: Mapping[str, Any]) -> dict[str, Any]:
    normalized = text.strip()
    match = _JSON_FENCE_RE.match(normalized)
    if match:
        normalized = match.group(1).strip()
    try:
        value = json.loads(normalized)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StructuredProviderError("Structured API output is not valid JSON", raw_response=dict(raw_response)) from exc
    if not isinstance(value, Mapping):
        raise StructuredProviderError("Structured API JSON output must be an object", raw_response=dict(raw_response))
    return dict(value)


def _provider_error(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        return _text(error.get("message"))
    return _text(payload.get("message"))


def _provider_error_code(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        return _text(error.get("code")) or _text(error.get("type"))
    return _text(payload.get("code"))


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = [
    "ChatAdapter",
    "ResponsesAdapter",
    "StructuredAdapter",
    "StructuredApiStyle",
    "StructuredClient",
    "StructuredProviderError",
    "StructuredResult",
    "SupportsPost",
]
