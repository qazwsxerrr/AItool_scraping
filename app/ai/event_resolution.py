"""Narrow ambiguity resolver used by Stage C event clustering.

This module deliberately exposes only merge/separate evidence.  It is not an
item-analysis skill and must never invent event title, summary, topic, or
other editorial fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

import httpx

from app.config.settings import Settings


class _SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


@dataclass(frozen=True)
class EventResolution:
    decision: str
    confidence: int
    reason: str | None = None
    raw: Any = None

    @property
    def merge(self) -> bool:
        return self.decision == "merge"

    @property
    def separate(self) -> bool:
        return self.decision == "separate"


class EventResolutionClient:
    """Provider adapter for ambiguity evidence, separate from item skills."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = 30.0,
        http_client: _SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = _normalize_style(api_style)
        self.timeout_seconds = float(timeout_seconds)
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: _SupportsPost | None = None) -> "EventResolutionClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            api_style=settings.ai_review_api_style,
            timeout_seconds=settings.ai_review_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def resolve_event(self, values: Iterable[Mapping[str, Any]]) -> EventResolution:
        if not self.is_configured:
            raise RuntimeError("Event resolver API is not configured")
        rows = [dict(value) for value in values]
        payload = self._payload(rows)
        response = self._post(payload)
        raw = _response_json(response)
        return parse_event_resolution(raw)

    def _payload(self, values: list[dict[str, Any]]) -> dict[str, Any]:
        instruction = (
            "Compare the supplied reports only for event identity. Return strict JSON "
            "with decision merge or separate, confidence 0-100, and short evidence. "
            "Do not generate title, summary, topic, or any editorial copy."
        )
        user = json.dumps(values, ensure_ascii=False, default=str)
        if self.api_style == "openai_chat":
            return {
                "model": self.model,
                "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            }
        if self.api_style == "openai_responses":
            return {
                "model": self.model,
                "input": [{"role": "system", "content": instruction}, {"role": "user", "content": user}],
                "text": {"format": {"type": "json_object"}},
            }
        return {
            "model": self.model,
            "task": "event_resolution",
            "instructions": instruction,
            "input": values,
            "response_schema": {"type": "object", "required": ["decision", "confidence"], "additionalProperties": True},
        }

    def _post(self, payload: dict[str, Any]):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"}
        if self._http_client is not None:
            try:
                response = self._http_client.post(self._endpoint(), headers=headers, json=payload, timeout=self.timeout_seconds)
            except TypeError:
                response = self._http_client.post(self._endpoint(), headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=True, trust_env=True) as client:
                response = client.post(self._endpoint(), headers=headers, json=payload)
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        return response

    def _endpoint(self) -> str:
        if self.api_url is None:
            raise RuntimeError("Event resolver API is not configured")
        if self.api_style == "openai_chat" and not self.api_url.casefold().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.casefold().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url


def event_resolution_client_from_settings(settings: Settings, http_client: _SupportsPost | None = None) -> EventResolutionClient:
    """Build the external resolver from the shared AI_REVIEW_* settings.

    Configuration is checked when an ambiguous group is actually resolved;
    constructing the adapter itself must remain side-effect free so callers
    can inject a test resolver or run deterministic groups without a provider.
    """

    return EventResolutionClient.from_settings(settings, http_client=http_client)


def resolve_event_group(values: Iterable[Mapping[str, Any]], resolver: Callable[..., Any]) -> EventResolution:
    """Ask a narrow resolver for merge/separate evidence.

    Resolver adapters may accept the complete group or two values.  Any
    malformed/failed response is represented as ``unknown`` so callers can
    retain deterministic provenance without treating model text as fact.
    """

    rows = [dict(value) for value in values]
    if not rows:
        return EventResolution("unknown", 0, "empty_group")
    try:
        raw = resolver(rows)
        parsed = parse_event_resolution(raw)
        if parsed.decision != "unknown":
            return parsed
    except TypeError:
        pass
    except Exception as exc:
        return EventResolution("unknown", 0, "resolver_failed", {"error": str(exc)})

    if len(rows) < 2:
        return EventResolution("unknown", 0, "resolver_no_decision")
    decisions: list[EventResolution] = []
    for index in range(1, len(rows)):
        try:
            parsed = parse_event_resolution(resolver(rows[0], rows[index]))
        except Exception as exc:
            decisions.append(EventResolution("unknown", 0, "resolver_failed", {"error": str(exc)}))
            continue
        decisions.append(parsed)
    if decisions and all(value.merge for value in decisions):
        return EventResolution("merge", min(value.confidence for value in decisions), "pairwise_merge", decisions)
    if decisions and any(value.separate for value in decisions):
        return EventResolution("separate", max(value.confidence for value in decisions), "pairwise_separate", decisions)
    return EventResolution("unknown", 0, "resolver_no_decision", decisions)


def parse_event_resolution(raw: Any) -> EventResolution:
    raw = _unwrap_provider_response(raw)
    data = dict(raw) if isinstance(raw, Mapping) else {}
    if isinstance(raw, str):
        data = {"decision": raw}
    if isinstance(raw, bool):
        data = {"decision": "merge" if raw else "separate"}
    value = data.get("decision") or data.get("resolution") or data.get("relation") or data.get("merge")
    if isinstance(value, bool):
        value = "merge" if value else "separate"
    decision = str(value).strip().casefold() if value is not None else "unknown"
    if decision in {"related", "merged", "same", "yes", "true"}:
        decision = "merge"
    elif decision in {"split", "unrelated", "different", "no", "false"}:
        decision = "separate"
    elif decision not in {"merge", "separate"}:
        decision = "unknown"
    try:
        confidence = max(0, min(100, int(float(data.get("confidence", data.get("score", 0))))))
    except (TypeError, ValueError, OverflowError):
        confidence = 0
    reason = data.get("reason") or data.get("evidence") or data.get("explanation")
    return EventResolution(decision, confidence, str(reason) if reason else None, raw)


def _normalize_style(value: Any) -> str:
    style = str(value or "generic_json").strip().casefold().replace("-", "_")
    style = {"chat": "openai_chat", "chat_completions": "openai_chat", "responses": "openai_responses", "openai_response": "openai_responses"}.get(style, style)
    if style not in {"generic_json", "openai_chat", "openai_responses"}:
        raise ValueError("api_style must be generic_json, openai_chat, or openai_responses")
    return style


def _response_json(response: Any) -> Any:
    value = response.json()
    return _unwrap_provider_response(value)


def _unwrap_provider_response(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    for key in ("output_parsed", "parsed", "result", "data"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    choices = value.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            try:
                return json.loads(content)
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"decision": content}
        if isinstance(content, Mapping):
            return content
    output = value.get("output")
    if isinstance(output, list):
        for entry in output:
            if not isinstance(entry, Mapping):
                continue
            content = entry.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, Mapping):
                        text = part.get("text") or part.get("value")
                        if isinstance(text, str):
                            try:
                                return json.loads(text)
                            except (TypeError, ValueError, json.JSONDecodeError):
                                return {"decision": text}
    return value


__all__ = ["EventResolution", "EventResolutionClient", "event_resolution_client_from_settings", "parse_event_resolution", "resolve_event_group"]
