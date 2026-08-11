from __future__ import annotations

import json

import pytest

from app.ai import ItemAnalysisClient
from app.ai.client import ItemAnalysisClient as CanonicalItemAnalysisClient
from app.ai.prompts import ITEM_ANALYSIS_RESPONSE_SCHEMA, ITEM_ANALYSIS_SYSTEM_PROMPT, PROJECT_SUMMARY_SYSTEM_PROMPT
from app.ai.schemas import ItemAnalysisRequest, ItemAnalysisResponse, apply_local_guard, parse_item_analysis_response
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


def test_generic_json_posts_item_and_normalizes_response():
    http = FakeHttpClient(
        FakeResponse(
            {
                "keep": "true",
                "content_class": "project_tool",
                "summary_cn": "  一个 MCP 工具  ",
                "reason": "  有可复用代码  ",
                "risk_flags": "营销；缺少许可证, 营销",
                "needs_verification": "false",
                "official_url": "https://example.test/docs",
                "confidence": 130,
            }
        )
    )
    client = ItemAnalysisClient.from_settings(
        Settings(
            ai_review_api_url="https://ai.example.test/analyze",
            ai_review_api_key="key",
            ai_review_model="model",
        ),
        http_client=http,
    )

    result = client.analyze(_request())

    assert isinstance(result, ItemAnalysisResponse)
    assert result.keep is True
    assert result.content_class == "project_tool"
    assert result.summary_cn == "一个 MCP 工具"
    assert result.reason == "有可复用代码"
    assert result.risk_flags == ["营销", "缺少许可证"]
    assert result.needs_verification is False
    assert result.confidence == 100
    assert result.raw_response is not None
    assert result.raw_response["keep"] == "true"
    call = http.calls[0]
    assert call["url"] == "https://ai.example.test/analyze"
    assert call["headers"]["Authorization"] == "Bearer key"
    assert call["json"]["task"] == "ai_item_analysis"
    assert call["json"]["item"]["item_id"] == 42
    assert len(http.calls) == 1


def test_source_content_class_is_authoritative_and_score_clamps():
    http = FakeHttpClient(
        FakeResponse(
            {
                "keep": False,
                "content_class": "made_up_class",
                "summary_cn": None,
                "reason": 123,
                "risk_flags": ["  broken link ", "", "broken link"],
                "needs_verification": 1,
                "official_url": "not-a-url",
                "confidence": -20,
            }
        )
    )
    client = ItemAnalysisClient(
        api_url="https://ai.example.test/analyze",
        api_key="key",
        http_client=http,
    )

    result = client.analyze(_request(source_content_class="official_model_company"))

    assert result.content_class == "official_model_company"
    assert result.summary_cn == ""
    assert result.reason == "123"
    assert result.risk_flags == ["broken link"]
    assert result.needs_verification is True
    assert result.official_url is None
    assert result.confidence == 0


def test_openai_chat_endpoint_and_json_fence_are_supported():
    content = "```json\n" + json.dumps(
        {
            "keep": True,
            "content_class": "community_social",
            "summary_cn": "社区线索",
            "reason": "需要进一步核实",
            "risk_flags": ["social-only"],
            "needs_verification": True,
            "official_url": None,
            "confidence": "88",
        },
        ensure_ascii=False,
    ) + "\n```"
    http = FakeHttpClient(FakeResponse({"choices": [{"message": {"content": content}}]}))
    client = ItemAnalysisClient.from_settings(
        Settings(
            ai_review_api_url="https://api.example.test/v1",
            ai_review_api_key="key",
            ai_review_model="chat-model",
            ai_review_api_style="openai_chat",
        ),
        http_client=http,
    )

    result = client.analyze(_request())

    call = http.calls[0]
    assert call["url"] == "https://api.example.test/v1/chat/completions"
    assert call["json"]["model"] == "chat-model"
    assert call["json"]["response_format"] == {"type": "json_object"}
    # The model proposed community_social, but registry/source routing wins.
    assert result.content_class == "project_tool"
    assert result.confidence == 88
    assert result.raw_response == http.response.payload
    assert "community_social" in result.raw_response["choices"][0]["message"]["content"]


def test_project_summary_parses_narrow_openai_response_without_keep_gate():
    content = json.dumps(
        {
            "summary_cn": "一个可组合的 AI 工作流项目。",
            "capabilities": ["编排模型调用"],
            "use_cases": ["搭建内部自动化流程"],
            "risk_flags": ["README 信息可能过时"],
        },
        ensure_ascii=False,
    )
    http = FakeHttpClient(FakeResponse({"choices": [{"message": {"content": content}}]}))
    client = ItemAnalysisClient.from_settings(
        Settings(
            ai_review_api_url="https://api.example.test/v1",
            ai_review_api_key="key",
            ai_review_model="chat-model",
            ai_review_api_style="openai_chat",
        ),
        http_client=http,
    )

    result = client.summarize_project(_request())

    assert result.keep is False
    assert result.content_class == "project_tool"
    assert "编排模型调用" in result.summary_cn
    assert result.risk_flags == ["README 信息可能过时"]
    assert PROJECT_SUMMARY_SYSTEM_PROMPT in http.calls[0]["json"]["messages"][0]["content"]
    assert len(http.calls) == 1


def test_missing_or_malformed_provider_json_is_rejected():
    http = FakeHttpClient(FakeResponse({"choices": [{"message": {"content": "not json"}}]}))
    client = ItemAnalysisClient(api_url="https://ai.example.test", api_key="key", http_client=http)

    with pytest.raises(ValueError, match="invalid JSON"):
        client.analyze(_request())


def test_provider_json_missing_required_analysis_field_is_rejected():
    http = FakeHttpClient(
        FakeResponse(
            {
                "keep": True,
                "content_class": "project_tool",
                "reason": "incomplete",
                "risk_flags": [],
                "needs_verification": False,
                "confidence": 50,
            }
        )
    )
    client = ItemAnalysisClient(api_url="https://ai.example.test", api_key="key", http_client=http)

    with pytest.raises(ValueError, match="missing required fields"):
        client.analyze(_request())


def test_request_rejects_invalid_source_class_and_non_dict_metrics():
    with pytest.raises(ValueError, match="source_content_class"):
        _request(source_content_class="unknown")
    with pytest.raises(TypeError, match="metrics"):
        _request(metrics=[])


def test_prompt_declares_analysis_is_not_fact_verification():
    assert "只能做三件事" in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "不要进行事实核实" in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "不要把你的摘要、理由、风险或链接当作事实" in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "official_model_company" in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "project_tool" in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "community_social" in ITEM_ANALYSIS_SYSTEM_PROMPT
    assert "必须原样使用输入的 source_content_class" in ITEM_ANALYSIS_SYSTEM_PROMPT


def test_public_modules_expose_the_single_analysis_client():
    assert ItemAnalysisClient is CanonicalItemAnalysisClient
    assert ITEM_ANALYSIS_RESPONSE_SCHEMA["confidence"] == "integer 0-100"


def test_local_guard_requires_verification_for_official_and_social_items():
    official = parse_item_analysis_response(
        {
            "keep": True,
            "content_class": "official_model_company",
            "summary_cn": "发布线索",
            "reason": "官方候选",
            "risk_flags": [],
            "needs_verification": False,
            "confidence": 90,
        },
        "official_model_company",
    )
    social = apply_local_guard(
        ItemAnalysisResponse(
            keep=True,
            content_class="community_social",
            summary_cn="讨论",
            reason="线索",
            risk_flags=[],
            needs_verification=False,
            confidence=70,
        )
    )

    assert official.needs_verification is True
    assert social.needs_verification is True
