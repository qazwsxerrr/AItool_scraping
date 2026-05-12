from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import Settings


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]): ...


@dataclass(frozen=True)
class ClaimExtractRequest:
    candidate_id: int
    title: str
    url: str | None
    source_group: str
    candidate_score: int
    ai_score: int
    ai_category: str | None
    body_preview: str
    matched_keywords: list[str]


@dataclass(frozen=True)
class ClaimExtractResponse:
    entity_name: str | None
    entity_type: str | None
    official_url: str | None
    github_url: str | None
    huggingface_url: str | None
    producthunt_url: str | None
    main_claims: list[str]
    release_signal: bool
    actionable_signal: bool
    confidence: int
    raw_response: dict[str, Any] | None = None


class AIClaimExtractClient:
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
        self.api_style = api_style
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "AIClaimExtractClient":
        return cls(
            api_url=settings.claim_extract_api_url,
            api_key=settings.claim_extract_api_key,
            model=settings.claim_extract_model,
            api_style=settings.claim_extract_api_style,
            timeout_seconds=settings.claim_extract_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def extract(self, request: ClaimExtractRequest) -> ClaimExtractResponse:
        if not self.is_configured:
            raise RuntimeError("Claim extract API is not configured")

        payload = self._build_payload(request)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = self._endpoint_url()
        if self._http_client is not None:
            response = self._http_client.post(url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return _parse_claim_response(response.json())

    def _endpoint_url(self) -> str:
        assert self.api_url is not None
        if self.api_style == "openai_chat" and not self.api_url.endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        return self.api_url

    def _build_payload(self, request: ClaimExtractRequest) -> dict[str, Any]:
        schema = {
            "entity_name": "string|null",
            "entity_type": "ai_tool|workflow|mcp|skill|api_proxy|model_release|tutorial|other|null",
            "official_url": "string|null",
            "github_url": "string|null",
            "huggingface_url": "string|null",
            "producthunt_url": "string|null",
            "main_claims": "array<string>",
            "release_signal": "boolean",
            "actionable_signal": "boolean",
            "confidence": "integer 0-100",
        }
        if self.api_style == "openai_chat":
            return {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 AI 工具情报 claim 抽取器。只返回 JSON，不要 Markdown。"
                            "从候选标题、URL、来源、AI 初筛和正文预览中抽取明确的工具/模型/MCP/workflow/skill 实体。"
                            "如果没有明确实体，entity_name=null 且 confidence<=40。"
                            "不要把泛讨论、求推荐、纯 benchmark 或营销列表误判为明确实体。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(asdict(request), ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
        return {
            "model": self.model,
            "task": "ai_tool_intel_claim_extract",
            "candidate": asdict(request),
            "response_schema": schema,
        }


def _parse_claim_response(data: dict[str, Any]) -> ClaimExtractResponse:
    result = _unwrap_response(data)
    claims = result.get("main_claims", result.get("claims", []))
    if not isinstance(claims, list):
        claims = []
    return ClaimExtractResponse(
        entity_name=_optional_str(result.get("entity_name")),
        entity_type=_optional_str(result.get("entity_type")),
        official_url=_optional_str(result.get("official_url")),
        github_url=_optional_str(result.get("github_url")),
        huggingface_url=_optional_str(result.get("huggingface_url")),
        producthunt_url=_optional_str(result.get("producthunt_url")),
        main_claims=[str(item) for item in claims],
        release_signal=bool(result.get("release_signal", False)),
        actionable_signal=bool(result.get("actionable_signal", False)),
        confidence=_clamp_int(result.get("confidence"), default=0),
        raw_response=data,
    )


def _unwrap_response(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("result"), dict):
        return data["result"]
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            parsed = json.loads(_strip_json_fence(content))
            if isinstance(parsed, dict):
                return parsed
    return data


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clamp_int(value: Any, *, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(0, min(number, 100))
