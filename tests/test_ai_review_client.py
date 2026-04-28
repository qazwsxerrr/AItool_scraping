from __future__ import annotations

import json

import pytest

from app.ai.review_client import AIReviewClient, AIReviewRequest, AIReviewResponse
from app.config.settings import Settings


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def test_ai_review_client_is_disabled_without_endpoint_or_key():
    settings = Settings(ai_review_api_url=None, ai_review_api_key=None)

    client = AIReviewClient.from_settings(settings)

    assert client.is_configured is False


def test_ai_review_client_posts_candidate_payload_and_parses_json_response():
    settings = Settings(
        ai_review_api_url="https://ai.example.local/review",
        ai_review_api_key="secret-key",
        ai_review_model="review-model",
    )
    http_client = FakeHttpClient(
        FakeResponse(
            {
                "keep": True,
                "score": 82,
                "category": "model_release",
                "reason": "Open weights model release with benchmark signal",
                "summary_cn": "开源权重模型发布，包含 benchmark 信号。",
            }
        )
    )
    client = AIReviewClient.from_settings(settings, http_client=http_client)

    response = client.review(
        AIReviewRequest(
            candidate_id=123,
            title="Released open weights GGUF model",
            url="https://huggingface.co/example/model",
            source_group="reddit_local_llama",
            candidate_score=88,
            body_preview="New benchmark and repo release",
            matched_keywords=["gguf", "benchmark"],
        )
    )

    assert isinstance(response, AIReviewResponse)
    assert response.keep is True
    assert response.score == 82
    assert response.summary_cn.startswith("开源权重模型")
    assert http_client.calls[0]["url"] == "https://ai.example.local/review"
    assert http_client.calls[0]["headers"]["Authorization"] == "Bearer secret-key"
    assert http_client.calls[0]["json"]["model"] == "review-model"
    assert http_client.calls[0]["json"]["candidate"]["candidate_id"] == 123


def test_ai_review_client_requires_configuration_before_review():
    client = AIReviewClient.from_settings(Settings(ai_review_api_url=None, ai_review_api_key=None))

    with pytest.raises(RuntimeError, match="AI review API is not configured"):
        client.review(
            AIReviewRequest(
                candidate_id=1,
                title="title",
                url=None,
                source_group="reddit_local_llama",
                candidate_score=50,
                body_preview="body",
                matched_keywords=[],
            )
        )
