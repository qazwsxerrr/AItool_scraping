from __future__ import annotations

import json

import pytest

from app.ai import ItemAnalysisClient, RawIntelEnvelope
from app.ai.client import ItemAnalysisClient as CanonicalItemAnalysisClient
from app.ai.prompts import ITEM_ANALYSIS_RESPONSE_SCHEMA, ITEM_ANALYSIS_SYSTEM_PROMPT, PROJECT_SUMMARY_SYSTEM_PROMPT
from app.ai.schemas import ItemAnalysisRequest, ItemAnalysisResponse, parse_item_analysis_response
from app.config.settings import Settings


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def _request(**overrides) -> ItemAnalysisRequest:
    values = {
        "item_id": 42,
        "title": "Example MCP tool",
        "url": "https://example.test/project",
        "source_id": "github_active",
        "source_content_class": "project_tool",
        "body_preview": "A reusable MCP server.",
        "metrics": {"stars": 1200, "pushed_at": "2026-08-01T00:00:00Z"},
    }
    values.update(overrides)
    return ItemAnalysisRequest(**values)


def _envelope(**overrides) -> RawIntelEnvelope:
    values = {
        "item_id": 42,
        "title": "Example MCP tool",
        "url": "https://example.test/project",
        "source_id": "github_active",
        "source_content_class": "project_tool",
        "source_group": "github_search",
        "body_text": "A reusable MCP server.",
    }
    values.update(overrides)
    return RawIntelEnvelope(**values)


def _triage_payload(**overrides):
    values = {
        "keep": True,
        "topic": "project",
        "summary_cn": "一个 MCP 工具",
        "keywords": ["MCP", "开源"],
        "selection_score": 87,
        "scores": {"relevance": 90, "impact": 80, "total": 87},
        "novelty": "new",
        "paper_support": {"is_paper": False},
        "risk_flags": [],
        "confidence": 91,
    }
    values.update(overrides)
    return values


def test_generic_json_posts_item_and_normalizes_response():
    http = FakeHttpClient(FakeResponse(_triage_payload(
        keep="true", summary_cn="  一个 MCP 工具  ", reason="  有可复用代码  ",
        risk_flags="营销；缺少许可证, 营销", confidence=130,
    )))
    client = ItemAnalysisClient.from_settings(
        Settings(ai_review_api_url="https://ai.example.test/triage", ai_review_api_key="key", ai_review_model="model"),
        http_client=http,
    )
    result = client.triage(_envelope())
    assert result.keep is True
    assert result.summary_cn == "一个 MCP 工具"
    assert result.risk_flags == ["营销", "缺少许可证"]
    assert result.confidence == 100
    assert result.raw_response and "keep" in result.raw_response
    assert http.calls[0]["json"]["task"] == "intel_triage"


def test_triage_uses_fixed_topic_taxonomy():
    http = FakeHttpClient(FakeResponse(_triage_payload(
        topic="paper", summary_cn="一篇研究摘要", reason="研究价值", confidence=91,
    )))
    settings = Settings(
        ai_review_api_url="https://ai.example.test/triage",
        ai_review_api_key="key",
        ai_review_model="model",
    )
    result = ItemAnalysisClient.from_settings(settings, http_client=http).triage(
        _envelope(source_content_class="official_model_company")
    )
    assert result.topic == "paper"
    assert http.calls[0]["json"]["task"] == "intel_triage"


def test_source_content_class_is_authoritative_and_score_clamps():
    http = FakeHttpClient(FakeResponse(_triage_payload(
        keep=False, topic="product", summary_cn=None, reason=123,
        risk_flags=[" broken link ", "", "broken link"], confidence=-20,
    )))
    result = ItemAnalysisClient(api_url="https://ai.example.test/triage", api_key="key", http_client=http).triage(
        _envelope(source_content_class="official_model_company")
    )
    assert result.content_class == "official_model_company"
    assert result.summary_cn == ""
    assert result.reason == "123"
    assert result.risk_flags == ["broken link"]
    assert result.confidence == 0


def test_openai_chat_and_responses_envelopes_are_supported():
    content = "```json\n" + json.dumps(_triage_payload(
        topic="opinion", summary_cn="社区线索", reason="来源材料摘要",
        risk_flags=["social-only"], confidence="88",
    ), ensure_ascii=False) + "\n```"
    http = FakeHttpClient(FakeResponse({"choices": [{"message": {"content": content}}]}))
    client = ItemAnalysisClient.from_settings(
        Settings(ai_review_api_url="https://api.example.test/v1", ai_review_api_key="key", ai_review_model="chat-model", ai_review_api_style="openai_chat"),
        http_client=http,
    )
    result = client.triage(_envelope())
    assert http.calls[0]["url"].endswith("/chat/completions")
    assert result.content_class == "project_tool"
    assert result.confidence == 88

    payload = _triage_payload(topic="opinion", summary_cn="Responses API 社区线索", reason="来源材料摘要", confidence=82)
    response_http = FakeHttpClient(FakeResponse({"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload, ensure_ascii=False)}]}]}))
    response_client = ItemAnalysisClient.from_settings(
        Settings(ai_review_api_url="https://api.example.test/v1", ai_review_api_key="key", ai_review_model="response-model", ai_review_api_style="openai_responses"),
        http_client=response_http,
    )
    assert response_client.triage(_envelope(source_content_class="official_model_company")).summary_cn == "Responses API 社区线索"
    assert response_http.calls[0]["url"].endswith("/responses")


def test_project_summary_uses_narrow_contract():
    content = json.dumps({"summary_cn": "一个项目。", "capabilities": ["编排模型调用"], "use_cases": ["自动化"], "risk_flags": []}, ensure_ascii=False)
    http = FakeHttpClient(FakeResponse({"choices": [{"message": {"content": content}}]}))
    client = ItemAnalysisClient.from_settings(
        Settings(ai_review_api_url="https://api.example.test/v1", ai_review_api_key="key", ai_review_model="chat-model", ai_review_api_style="openai_chat"),
        http_client=http,
    )
    result = client.summarize_project(_request())
    assert result.keep is False
    assert "编排模型调用" in result.summary_cn
    assert PROJECT_SUMMARY_SYSTEM_PROMPT in http.calls[0]["json"]["messages"][0]["content"]


def test_malformed_or_incomplete_provider_json_is_rejected():
    client = ItemAnalysisClient(api_url="https://ai.example.test", api_key="key", http_client=FakeHttpClient(FakeResponse({"choices": [{"message": {"content": "not json"}}]})))
    with pytest.raises(ValueError, match="invalid JSON"):
        client.triage(_envelope())
    incomplete = FakeHttpClient(FakeResponse({"keep": True, "topic": "project", "summary_cn": "x", "keywords": [], "selection_score": 1, "novelty": "unknown", "risk_flags": []}))
    with pytest.raises(ValueError, match="missing required fields"):
        ItemAnalysisClient(api_url="https://ai.example.test", api_key="key", http_client=incomplete).triage(_envelope())


def test_prompt_and_public_exports_are_ai_only():
    assert ItemAnalysisClient is CanonicalItemAnalysisClient
    assert not hasattr(ItemAnalysisClient, "analyze")
    assert ITEM_ANALYSIS_RESPONSE_SCHEMA["confidence"] == "integer 0-100"
    assert "只能做三件事" in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "needs_verification" not in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "verification" not in ITEM_ANALYSIS_SYSTEM_PROMPT
    result = parse_item_analysis_response({"keep": True, "content_class": "official_model_company", "summary_cn": "摘要", "reason": "理由", "risk_flags": [], "confidence": 90}, "official_model_company")
    assert result.content_class == "official_model_company"
