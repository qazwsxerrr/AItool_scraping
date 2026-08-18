"""HTTP adapter for the one-shot Stage D editorial selection skill."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

import httpx

from app.config.settings import Settings

from .models import StageDEditorialResponse
from .parser import strict_parse_stage_d
from .prompts import build_stage_d_provider_payload, preflight_stage_d_schema


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


class StageDEditorialClient:
    """A dedicated provider client; it never reuses Stage A/B semantics."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = 45.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = _normalize_api_style(api_style)
        self.timeout_seconds = float(timeout_seconds)
        self._http_client = http_client
        self.last_raw_response: Any | None = None

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "StageDEditorialClient":
        return cls(
            api_url=settings.ai_stage_d_api_url,
            api_key=settings.ai_stage_d_api_key,
            model=settings.ai_stage_d_model,
            api_style=settings.ai_stage_d_api_style,
            timeout_seconds=settings.ai_stage_d_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def select_events(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        edition: Mapping[str, Any],
        total_max: int = 30,
    ) -> StageDEditorialResponse:
        if not self.is_configured:
            raise RuntimeError("Stage D editorial API is not configured")
        preflight_stage_d_schema()
        event_ids = [int(event["event_id"]) for event in events]
        response = self._post_once(
            self._endpoint_url(),
            build_stage_d_provider_payload(events, edition=edition, model=self.model, api_style=self.api_style),
        )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("Stage D API returned invalid JSON") from exc
        self.last_raw_response = payload
        return strict_parse_stage_d(payload, event_ids=event_ids, total_max=total_max, events=events)

    def _post_once(self, url: str, payload: dict[str, Any]):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._http_client is not None:
            try:
                response = self._http_client.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
            except TypeError:
                response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=True, trust_env=True) as client:
                response = client.post(url, headers=headers, json=payload)
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        return response

    def _endpoint_url(self) -> str:
        if self.api_url is None:  # pragma: no cover - guarded by is_configured
            raise RuntimeError("Stage D editorial API is not configured")
        if self.api_style == "openai_chat" and not self.api_url.casefold().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.casefold().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url


def _normalize_api_style(value: Any) -> str:
    style = str(value or "generic_json").strip().casefold().replace("-", "_")
    style = {"responses": "openai_responses", "openai_response": "openai_responses", "chat": "openai_chat", "chat_completions": "openai_chat"}.get(style, style)
    if style not in {"generic_json", "openai_chat", "openai_responses"}:
        raise ValueError("api_style must be generic_json, openai_chat, or openai_responses")
    return style


__all__ = ["StageDEditorialClient", "SupportsPost"]
