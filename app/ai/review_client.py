from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import Settings


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]): ...


@dataclass(frozen=True)
class AIReviewRequest:
    candidate_id: int
    title: str
    url: str | None
    source_group: str
    candidate_score: int
    body_preview: str
    matched_keywords: list[str]


@dataclass(frozen=True)
class AIReviewResponse:
    keep: bool
    score: int
    category: str | None = None
    reason: str | None = None
    summary_cn: str | None = None
    raw_response: dict[str, Any] | None = None


class AIReviewClient:
    """Small configurable HTTP client for the later AI first-screening step."""

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
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "AIReviewClient":
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

    def review(self, request: AIReviewRequest) -> AIReviewResponse:
        if not self.is_configured:
            raise RuntimeError("AI review API is not configured")

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
        data = response.json()
        return _parse_review_response(data)

    def _endpoint_url(self) -> str:
        assert self.api_url is not None
        if self.api_style == "openai_chat" and not self.api_url.endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        return self.api_url

    def _build_payload(self, request: AIReviewRequest) -> dict[str, Any]:
        if self.api_style == "openai_chat":
            return {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是严格的 AI 工具情报初筛器。只返回 JSON，不要 Markdown。"
                            "目标只保留两类内容："
                            "1) 好用的 AI 工具、agent 工作流、MCP server/client、skill/skills、"
                            "prompt/workflow、OpenAI-compatible API、2API、反代/中转/API gateway、"
                            "模型部署或调用工具；"
                            "2) 明确的新 AI 进展，例如新模型发布、开源权重发布、重要产品/能力发布。"
                            "明确排除：泛 benchmark、纯模型横评、硬件功耗/VRAM/吞吐调优、"
                            "观点讨论、吐槽、社区新闻、融资故事、个人经历、问题求推荐、"
                            "个人部署踩坑/性能瓶颈复盘、没有可复用工具或明确发布的教程。"
                            "即使包含 LLM/model/benchmark/Qwen/Claude 等词，只要不是工具/工作流/MCP/2API/反代"
                            "或明确新模型/新能力发布，也必须 keep=false 且 score<=40。"
                            "category 只能使用：ai_tool, workflow, mcp, skill, api_proxy, model_release, product_release, tutorial, other。"
                            "返回字段：keep(boolean), score(integer 0-100), "
                            "category(string|null), reason(string|null), summary_cn(string|null)。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(asdict(request), ensure_ascii=False),
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            }
        return {
            "model": self.model,
            "task": "ai_tool_intel_first_screening",
            "candidate": asdict(request),
            "response_schema": {
                "keep": "boolean",
                "score": "integer 0-100",
                "category": "string|null",
                "reason": "string|null",
                "summary_cn": "string|null",
            },
        }


def _parse_review_response(data: dict[str, Any]) -> AIReviewResponse:
    """Parse a provider-neutral JSON response.

    The configured endpoint should return the schema directly. If a wrapper is used,
    put the object under `result`.
    """
    result = _unwrap_response(data)
    return AIReviewResponse(
        keep=bool(result.get("keep", False)),
        score=int(result.get("score", 0)),
        category=result.get("category"),
        reason=result.get("reason"),
        summary_cn=result.get("summary_cn"),
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
