"""Unified one-call AI client for normalized intelligence item analysis."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.ai.prompts import (
    ITEM_ANALYSIS_RESPONSE_SCHEMA,
    ITEM_ANALYSIS_SYSTEM_PROMPT,
    ITEM_ANALYSIS_TASK,
    PROJECT_SUMMARY_SYSTEM_PROMPT,
    PROJECT_SUMMARY_TASK,
    build_generic_json_payload,
    build_openai_chat_payload,
    build_openai_responses_payload,
)
from app.ai.schemas import (
    COMMUNITY_SOCIAL,
    CONTENT_CLASSES,
    ContentClass,
    ItemAnalysisRequest,
    ItemAnalysisResponse,
    OFFICIAL_MODEL_COMPANY,
    PROJECT_SUMMARY_RESPONSE_SCHEMA,
    PROJECT_TOOL,
    parse_item_analysis_response,
    parse_project_summary_response,
)
from app.config.settings import Settings


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]): ...


class ItemAnalysisClient:
    """Analyze one item with exactly one provider request.

    Retries and multi-stage model work are intentionally absent from this
    boundary.  A job may record and retry a failed item later, but one invocation
    of :meth:`analyze` performs only one HTTP call.
    """

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
        style = _normalize_api_style(api_style)
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = style
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http_client: SupportsPost | None = None,
    ) -> "ItemAnalysisClient":
        """Build a client from the configured single-pass AI provider."""

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

    def analyze(self, request: ItemAnalysisRequest) -> ItemAnalysisResponse:
        if not self.is_configured:
            raise RuntimeError("Item analysis API is not configured")
        if not isinstance(request, ItemAnalysisRequest):
            raise TypeError("request must be an ItemAnalysisRequest")

        response = self._post_once(self._endpoint_url(), self._build_payload(request))
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            raise ValueError("Item analysis API returned invalid JSON") from exc
        return parse_item_analysis_response(data, request.source_content_class)

    def summarize_project(self, request: ItemAnalysisRequest) -> ItemAnalysisResponse:
        """Run one GitHub project-summary request using the same schema/audit path."""

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
            # The narrow contract may arrive directly, inside a generic
            # envelope, or inside an OpenAI-compatible choices message.  The
            # project parser already knows how to unwrap all of those shapes.
            try:
                return parse_project_summary_response(data)
            except ValueError:
                raise analysis_error

    def _post_once(self, url: str, payload: dict[str, Any]):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._http_client is not None:
            try:
                response = self._http_client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                )
            except TypeError:
                # Small test/fake clients often expose only the minimal
                # ``post(url, headers, json)`` signature.
                response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                http2=True,
                trust_env=True,
            ) as client:
                response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response

    def _endpoint_url(self) -> str:
        if self.api_url is None:  # pragma: no cover - guarded by is_configured
            raise RuntimeError("Item analysis API is not configured")
        if self.api_style == "openai_chat" and not self.api_url.lower().endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        if self.api_style == "openai_responses" and not self.api_url.lower().endswith("/responses"):
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
            return build_openai_chat_payload(
                request,
                model=self.model,
                task=task,
                system_prompt=system_prompt,
                response_schema=response_schema,
            )
        if self.api_style == "openai_responses":
            return build_openai_responses_payload(
                request,
                model=self.model,
                task=task,
                system_prompt=system_prompt,
                response_schema=response_schema,
            )
        return build_generic_json_payload(
            request,
            model=self.model,
            task=task,
            system_prompt=system_prompt,
            response_schema=response_schema,
        )

__all__ = [
    "COMMUNITY_SOCIAL",
    "CONTENT_CLASSES",
    "ContentClass",
    "ITEM_ANALYSIS_RESPONSE_SCHEMA",
    "ITEM_ANALYSIS_SYSTEM_PROMPT",
    "ITEM_ANALYSIS_TASK",
    "PROJECT_SUMMARY_SYSTEM_PROMPT",
    "PROJECT_SUMMARY_TASK",
    "PROJECT_SUMMARY_RESPONSE_SCHEMA",
    "ItemAnalysisClient",
    "ItemAnalysisRequest",
    "ItemAnalysisResponse",
    "OFFICIAL_MODEL_COMPANY",
    "PROJECT_TOOL",
    "SupportsPost",
    "parse_item_analysis_response",
    "parse_project_summary_response",
]


def _normalize_api_style(value: Any) -> str:
    """Normalize provider style aliases while keeping one internal spelling."""

    style = str(value or "generic_json").strip().lower().replace("-", "_")
    aliases = {
        "responses": "openai_responses",
        "openai_response": "openai_responses",
        "openai_responses_api": "openai_responses",
        "chat": "openai_chat",
        "chat_completions": "openai_chat",
    }
    style = aliases.get(style, style)
    if style not in {"generic_json", "openai_chat", "openai_responses"}:
        raise ValueError("api_style must be generic_json, openai_chat, or openai_responses")
    return style
