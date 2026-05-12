from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import Settings


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]): ...


@dataclass(frozen=True)
class AIVerifyRequest:
    candidate: dict[str, Any]
    ai_review: dict[str, Any]
    extracted_claim: dict[str, Any] | None
    evidence_items: list[dict[str, Any]]
    source_quality: dict[str, Any]


@dataclass(frozen=True)
class AIVerifyResponse:
    verified: bool
    final_keep: bool
    final_score: int
    recommendation_level: str
    relevance_score: int
    usefulness_score: int
    credibility_score: int
    novelty_score: int
    reproducibility_score: int
    audience_fit_score: int
    source_quality_score: int
    spam_risk_score: int
    category: str | None
    summary_cn: str | None
    recommendation_reason: str | None
    risk_reason: str | None
    evidence_summary: list[str]
    risk_flags: list[str]
    raw_response: dict[str, Any] | None = None


class AIVerifyClient:
    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None = None,
        api_style: str = "generic_json",
        timeout_seconds: float = 60.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.api_style = api_style
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "AIVerifyClient":
        return cls(
            api_url=settings.ai_verify_api_url,
            api_key=settings.ai_verify_api_key,
            model=settings.ai_verify_model,
            api_style=settings.ai_verify_api_style,
            timeout_seconds=settings.ai_verify_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def verify(self, request: AIVerifyRequest) -> AIVerifyResponse:
        if not self.is_configured:
            raise RuntimeError("AI verify API is not configured")

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
        return _parse_verify_response(response.json())

    def _endpoint_url(self) -> str:
        assert self.api_url is not None
        if self.api_style == "openai_chat" and not self.api_url.endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        return self.api_url

    def _build_payload(self, request: AIVerifyRequest) -> dict[str, Any]:
        schema = {
            "verified": "boolean",
            "final_keep": "boolean",
            "final_score": "integer 0-100",
            "recommendation_level": "S|A|B|C|D",
            "relevance_score": "integer 0-100",
            "usefulness_score": "integer 0-100",
            "credibility_score": "integer 0-100",
            "novelty_score": "integer 0-100",
            "reproducibility_score": "integer 0-100",
            "audience_fit_score": "integer 0-100",
            "source_quality_score": "integer 0-100",
            "spam_risk_score": "integer 0-100",
            "category": "ai_tool|workflow|mcp|skill|api_proxy|model_release|product_release|tutorial|other",
            "summary_cn": "string|null",
            "recommendation_reason": "string|null",
            "risk_reason": "string|null",
            "evidence_summary": "array<string>",
            "risk_flags": "array<string>",
        }
        if self.api_style == "openai_chat":
            return {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是严格的 AI 工具情报核实器。只返回 JSON，不要 Markdown。"
                            "必须基于 evidence_items 判断，不得只根据标题或关键词推荐。"
                            "无证据时 credibility_score 不得高于 50；只有 Product Hunt/X 且无官网/文档/仓库时 final_score 不得高于 65。"
                            "纯社区讨论、求推荐、泛 benchmark、营销列表、虚假开源或空仓库必须降低分数并添加 risk_flags。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(asdict(request), ensure_ascii=False, default=str)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
        return {
            "model": self.model,
            "task": "ai_tool_intel_verify_with_evidence",
            "input": asdict(request),
            "response_schema": schema,
        }


def _parse_verify_response(data: dict[str, Any]) -> AIVerifyResponse:
    result = _unwrap_response(data)
    return AIVerifyResponse(
        verified=bool(result.get("verified", False)),
        final_keep=bool(result.get("final_keep", False)),
        final_score=_clamp_int(result.get("final_score")),
        recommendation_level=str(result.get("recommendation_level") or "D"),
        relevance_score=_clamp_int(result.get("relevance_score")),
        usefulness_score=_clamp_int(result.get("usefulness_score")),
        credibility_score=_clamp_int(result.get("credibility_score")),
        novelty_score=_clamp_int(result.get("novelty_score")),
        reproducibility_score=_clamp_int(result.get("reproducibility_score")),
        audience_fit_score=_clamp_int(result.get("audience_fit_score")),
        source_quality_score=_clamp_int(result.get("source_quality_score")),
        spam_risk_score=_clamp_int(result.get("spam_risk_score")),
        category=_optional_str(result.get("category")),
        summary_cn=_optional_str(result.get("summary_cn")),
        recommendation_reason=_optional_str(result.get("recommendation_reason")),
        risk_reason=_optional_str(result.get("risk_reason")),
        evidence_summary=_string_list(result.get("evidence_summary")),
        risk_flags=_string_list(result.get("risk_flags")),
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


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
