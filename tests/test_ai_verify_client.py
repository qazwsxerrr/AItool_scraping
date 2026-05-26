from __future__ import annotations

import json

from app.ai.verify_client import AIVerifyClient, AIVerifyRequest, AIVerifyResponse
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


def _request() -> AIVerifyRequest:
    return AIVerifyRequest(
        candidate={"candidate_id": 17, "title": "Example model release"},
        ai_review={"ai_keep": True, "ai_score": 95},
        extracted_claim={"entity_name": "Example", "main_claims": ["released"]},
        evidence_items=[{"url": "https://github.com/example/repo", "supports_claim": "support"}],
        source_quality={"quality_weight": 0.9},
        claim_verifications=[{"supports_claim": "support", "confidence": 90}],
    )


def test_ai_verify_client_supports_openai_chat_exact_schema_response():
    settings = Settings(
        ai_verify_api_url="https://api.deepseek.com",
        ai_verify_api_key="secret-key",
        ai_verify_model="deepseek-v4-flash",
        ai_verify_api_style="openai_chat",
    )
    http_client = FakeHttpClient(
        FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "verified": True,
                                    "final_keep": True,
                                    "final_score": 88,
                                    "recommendation_level": "A",
                                    "relevance_score": 90,
                                    "usefulness_score": 85,
                                    "credibility_score": 92,
                                    "novelty_score": 80,
                                    "reproducibility_score": 75,
                                    "audience_fit_score": 86,
                                    "source_quality_score": 88,
                                    "spam_risk_score": 10,
                                    "category": "model_release",
                                    "summary_cn": "示例模型发布。",
                                    "recommendation_reason": "证据充分。",
                                    "risk_reason": "无明显风险。",
                                    "evidence_summary": ["GitHub 支持发布声明"],
                                    "risk_flags": [],
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        )
    )
    client = AIVerifyClient.from_settings(settings, http_client=http_client)

    response = client.verify(_request())

    assert isinstance(response, AIVerifyResponse)
    assert response.verified is True
    assert response.final_keep is True
    assert response.final_score == 88
    assert response.relevance_score == 90
    assert response.usefulness_score == 85
    assert response.recommendation_level == "A"
    assert response.evidence_summary == ["GitHub 支持发布声明"]

    call = http_client.calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    system_prompt = call["json"]["messages"][0]["content"]
    assert "必须返回且只能返回包含以下字段的 JSON 对象" in system_prompt
    assert "relevance_score" in system_prompt
    assert "spam_risk_score" in system_prompt


def test_ai_verify_client_maps_legacy_recommendation_response_without_zeroing_dimensions():
    settings = Settings(
        ai_verify_api_url="https://api.deepseek.com",
        ai_verify_api_key="secret-key",
        ai_verify_model="deepseek-v4-flash",
        ai_verify_api_style="openai_chat",
    )
    http_client = FakeHttpClient(
        FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidate_id": 17,
                                    "recommendation": "keep",
                                    "credibility_score": 95,
                                    "ai_score": 100,
                                    "category": "3D生成模型",
                                    "risk_flags": [],
                                    "summary_cn": "微软开源 TRELLIS.2 模型。",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
        )
    )
    client = AIVerifyClient.from_settings(settings, http_client=http_client)

    response = client.verify(_request())

    assert response.verified is True
    assert response.final_keep is True
    assert response.final_score == 100
    assert response.recommendation_level == "S"
    assert response.credibility_score == 95
    assert response.relevance_score == 100
    assert response.usefulness_score == 100
    assert response.novelty_score == 100
    assert response.reproducibility_score == 100
    assert response.audience_fit_score == 100
    assert response.source_quality_score == 95
    assert response.spam_risk_score == 0
    assert response.summary_cn.startswith("微软开源")
