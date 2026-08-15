"""Unified provider client for project summaries and the two intelligence stages."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.ai.prompts import (
    ITEM_ANALYSIS_RESPONSE_SCHEMA,
    ITEM_ANALYSIS_SYSTEM_PROMPT,
    ITEM_ANALYSIS_TASK,
    PROJECT_SUMMARY_RESPONSE_SCHEMA,
    PROJECT_SUMMARY_SYSTEM_PROMPT,
    PROJECT_SUMMARY_TASK,
    build_generic_json_payload,
    build_openai_chat_payload,
    build_openai_responses_payload,
)
from app.ai.schemas import ItemAnalysisRequest, ItemAnalysisResponse, parse_item_analysis_response, parse_project_summary_response
from app.ai.skills.intel_triage import AnalysisResult, IntelTriageClient, RawIntelEnvelope, ScreenResult
from app.config.settings import Settings


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


class ItemAnalysisClient:
    """Shared provider configuration with Stage A ``screen`` and Stage B ``analyze`` APIs."""

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = _normalize_api_style(api_style)
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "ItemAnalysisClient":
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

    def screen(self, envelope: RawIntelEnvelope | dict[str, Any]) -> ScreenResult:
        return self._intel_client().screen(envelope)

    def analyze(self, envelope: RawIntelEnvelope | dict[str, Any]) -> AnalysisResult:
        return self._intel_client().analyze(envelope)

    def summarize_project(self, request: ItemAnalysisRequest) -> ItemAnalysisResponse:
        """Run the existing narrow GitHub summary contract."""

        if not self.is_configured:
            raise RuntimeError("Item analysis API is not configured")
        if not isinstance(request, ItemAnalysisRequest):
            raise TypeError("request must be an ItemAnalysisRequest")
        payload = self._build_payload(
            request,
            task=PROJECT_SUMMARY_TASK,
            system_prompt=PROJECT_SUMMARY_SYSTEM_PROMPT,
            response_schema=PROJECT_SUMMARY_RESPONSE_SCHEMA,
        )
        response = self._post_once(self._endpoint_url(), payload)
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("Item analysis API returned invalid JSON") from exc
        try:
            return parse_item_analysis_response(data, request.source_content_class)
        except ValueError as analysis_error:
            try:
                return parse_project_summary_response(data)
            except ValueError:
                raise analysis_error

    def _intel_client(self) -> IntelTriageClient:
        return IntelTriageClient(
            api_url=self.api_url,
            api_key=self.api_key,
            model=self.model,
            api_style=self.api_style,
            timeout_seconds=self.timeout_seconds,
            http_client=self._http_client,
        )

    def _post_once(self, url: str, payload: dict[str, Any]):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Accept": "application/json"}
        if self._http_client is not None:
            try:
                response = self._http_client.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
            except TypeError:
                response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=True, trust_env=True) as client:
                response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response

    def _endpoint_url(self) -> str:
        if self.api_url is None:  # pragma: no cover - guarded by is_configured
            raise RuntimeError("Item analysis API is not configured")
        if self.api_style == "openai_chat" and not self.api_url.casefold().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.casefold().endswith("/responses"):
            return f"{self.api_url}/responses"
        return self.api_url

    def _build_payload(
        self,
        request: ItemAnalysisRequest,
        *,
        task: str = ITEM_ANALYSIS_TASK,
        system_prompt: str = ITEM_ANALYSIS_SYSTEM_PROMPT,
        response_schema: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self.api_style == "openai_chat":
            return build_openai_chat_payload(request, model=self.model, task=task, system_prompt=system_prompt, response_schema=response_schema)
        if self.api_style == "openai_responses":
            return build_openai_responses_payload(request, model=self.model, task=task, system_prompt=system_prompt, response_schema=response_schema)
        return build_generic_json_payload(request, model=self.model, task=task, system_prompt=system_prompt, response_schema=response_schema)


def _normalize_api_style(value: Any) -> str:
    style = str(value or "generic_json").strip().casefold().replace("-", "_")
    style = {"responses": "openai_responses", "openai_response": "openai_responses", "chat": "openai_chat", "chat_completions": "openai_chat"}.get(style, style)
    if style not in {"generic_json", "openai_chat", "openai_responses"}:
        raise ValueError("api_style must be generic_json, openai_chat, or openai_responses")
    return style


__all__ = ["ItemAnalysisClient", "SupportsPost"]
