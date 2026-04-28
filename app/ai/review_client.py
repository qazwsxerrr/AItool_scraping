from __future__ import annotations

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
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "AIReviewClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            timeout_seconds=settings.ai_review_timeout_seconds,
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key)

    def review(self, request: AIReviewRequest) -> AIReviewResponse:
        if not self.is_configured:
            raise RuntimeError("AI review API is not configured")

        payload = {
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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if self._http_client is not None:
            response = self._http_client.post(self.api_url, headers=headers, json=payload)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                response = client.post(self.api_url, headers=headers, json=payload)

        response.raise_for_status()
        data = response.json()
        return _parse_review_response(data)


def _parse_review_response(data: dict[str, Any]) -> AIReviewResponse:
    """Parse a provider-neutral JSON response.

    The configured endpoint should return the schema directly. If a wrapper is used,
    put the object under `result`.
    """
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    return AIReviewResponse(
        keep=bool(result.get("keep", False)),
        score=int(result.get("score", 0)),
        category=result.get("category"),
        reason=result.get("reason"),
        summary_cn=result.get("summary_cn"),
        raw_response=data,
    )
