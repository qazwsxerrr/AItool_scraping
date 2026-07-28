from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import Settings
from app.github.project_types import GitHubProjectDigest, GitHubProjectProfile


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]): ...


@dataclass(frozen=True)
class GitHubDigestRequest:
    profile: dict[str, Any]


class GitHubProjectDigestClient:
    """AI client that turns GitHub metadata and README text into a readable project digest."""

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
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "GitHubProjectDigestClient":
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

    def digest(self, profile: GitHubProjectProfile) -> GitHubProjectDigest:
        if not self.is_configured:
            raise RuntimeError("GitHub digest AI API is not configured")

        request = GitHubDigestRequest(profile=_profile_payload(profile))
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
        return _parse_digest_response(response.json(), default_name=profile.repo_full_name)

    def _endpoint_url(self) -> str:
        assert self.api_url is not None
        if self.api_style == "openai_chat" and not self.api_url.endswith("/chat/completions"):
            return f"{self.api_url}/chat/completions"
        return self.api_url

    def _build_payload(self, request: GitHubDigestRequest) -> dict[str, Any]:
        if self.api_style == "openai_chat":
            return {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 GitHub AI 项目情报分析器。只返回 JSON，不要 Markdown。"
                            "你的任务不是事实核验，而是基于 GitHub repo metadata、README、topics、release 信息，"
                            "生成面向人类阅读的中文项目画像。不要因为缺少第三方证据就否定项目；"
                            "只在 README 或描述出现夸张、灰产、明显不可用时写风险。"
                            "返回字段：project_name, summary_cn, description_cn, keywords(list), "
                            "project_type, target_users(list), main_features(list), how_to_try, "
                            "risk_notes(list), is_ai_related(boolean), ai_relevance_score(0-100), "
                            "readme_quality_score(0-100), usability_score(0-100), digest_confidence(0-100)。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(asdict(request), ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            }
        return {
            "model": self.model,
            "task": "github_project_digest_cn",
            "request": asdict(request),
            "response_schema": {
                "project_name": "string",
                "summary_cn": "string",
                "description_cn": "string",
                "keywords": "list[string]",
                "project_type": "string",
                "target_users": "list[string]",
                "main_features": "list[string]",
                "how_to_try": "string|null",
                "risk_notes": "list[string]",
                "is_ai_related": "boolean",
                "ai_relevance_score": "integer 0-100",
                "readme_quality_score": "integer 0-100",
                "usability_score": "integer 0-100",
                "digest_confidence": "integer 0-100",
            },
        }


def _profile_payload(profile: GitHubProjectProfile) -> dict[str, Any]:
    data = asdict(profile)
    readme = data.get("readme_excerpt") or data.get("readme_text") or ""
    data["readme_excerpt"] = _truncate(str(readme), 8000)
    data.pop("readme_text", None)
    return data


def _parse_digest_response(data: dict[str, Any], *, default_name: str) -> GitHubProjectDigest:
    result = _unwrap_response(data)
    return GitHubProjectDigest(
        project_name=_text(result.get("project_name"), default_name),
        summary_cn=_text(result.get("summary_cn"), "未生成摘要。"),
        description_cn=_text(result.get("description_cn"), _text(result.get("summary_cn"), "未生成详细介绍。")),
        keywords=_string_list(result.get("keywords")),
        project_type=_text(result.get("project_type"), "unknown"),
        target_users=_string_list(result.get("target_users")),
        main_features=_string_list(result.get("main_features")),
        how_to_try=_optional_text(result.get("how_to_try")),
        risk_notes=_string_list(result.get("risk_notes")),
        is_ai_related=bool(result.get("is_ai_related", False)),
        ai_relevance_score=_score(result.get("ai_relevance_score")),
        readme_quality_score=_score(result.get("readme_quality_score")),
        usability_score=_score(result.get("usability_score")),
        digest_confidence=_score(result.get("digest_confidence")),
        digest_source="ai",
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
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _truncate(value: str, max_chars: int) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _score(value: Any) -> int:
    try:
        return max(0, min(int(value), 100))
    except (TypeError, ValueError):
        return 0
