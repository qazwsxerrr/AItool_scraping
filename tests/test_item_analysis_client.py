from __future__ import annotations

from app.ai import AnalysisResult, ItemAnalysisClient, ItemAnalysisRequest, RawIntelEnvelope, ScreenResult
from app.ai.client import ItemAnalysisClient as CanonicalItemAnalysisClient
from app.ai.prompts import PROJECT_SUMMARY_SYSTEM_PROMPT
from app.config.settings import Settings


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, **kwargs):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse(self.payload)


def _envelope(**overrides) -> RawIntelEnvelope:
    value = {
        "item_id": 42,
        "title": "Example MCP tool",
        "url": "https://example.test/project",
        "source_id": "github_active",
        "source_content_class": "project_tool",
        "source_group": "github_search",
        "body_text": "A reusable MCP server.",
    }
    value.update(overrides)
    return RawIntelEnvelope(**value)


def _request() -> ItemAnalysisRequest:
    return ItemAnalysisRequest(
        item_id=42,
        title="Example MCP tool",
        url="https://example.test/project",
        source_id="github_active",
        source_content_class="project_tool",
        body_preview="A reusable MCP server.",
        metrics={"stars": 1200},
    )


def _analysis_payload():
    return {
        "topic": "project",
        "topics": ["project"],
        "summary_cn": "一个 MCP 工具",
        "keywords": ["MCP"],
        "entities": [{"type": "technology", "name": "MCP"}],
        "selection_score": 88,
        "score_components": {"relevance": 90, "impact": 80, "freshness": 85, "source_authority": 70, "actionability": 80, "total": 88},
        "paper_support": {"is_paper": False},
        "risk_flags": [],
        "reason": "项目材料",
        "confidence": 90,
    }


def test_item_analysis_client_exposes_only_screen_and_analyze_for_intelligence():
    screen_http = FakeHttp({"decision": "pass", "reason_code": "relevant", "reason": "ok", "confidence": 90, "risk_flags": []})
    client = ItemAnalysisClient.from_settings(
        Settings(ai_review_api_url="https://ai.example.test", ai_review_api_key="key", ai_review_model="model"),
        http_client=screen_http,
    )
    screened = client.screen(_envelope())
    assert isinstance(screened, ScreenResult)
    assert screened.decision == "pass"
    assert screen_http.calls[0]["json"]["task"] == "intel_screen"
    assert not hasattr(client, "triage")

    analysis_http = FakeHttp(_analysis_payload())
    analysis_client = ItemAnalysisClient(
        api_url="https://ai.example.test",
        api_key="key",
        http_client=analysis_http,
    )
    analyzed = analysis_client.analyze(_envelope())
    assert isinstance(analyzed, AnalysisResult)
    assert analyzed.selection_score == 88
    assert analysis_http.calls[0]["json"]["task"] == "intel_analysis"


def test_project_summary_contract_remains_separate_from_intelligence_stages():
    http = FakeHttp({
        "choices": [{"message": {"content": '{"summary_cn":"一个项目。","capabilities":["编排模型调用"],"use_cases":["自动化"],"risk_flags":[]}'}}]
    })
    client = ItemAnalysisClient.from_settings(
        Settings(ai_review_api_url="https://api.example.test/v1", ai_review_api_key="key", ai_review_model="chat-model", ai_review_api_style="openai_chat"),
        http_client=http,
    )
    result = client.summarize_project(_request())
    assert "编排模型调用" in result.summary_cn
    assert PROJECT_SUMMARY_SYSTEM_PROMPT in http.calls[0]["json"]["messages"][0]["content"]
    assert http.calls[0]["url"].endswith("/chat/completions")


def test_configured_client_rejects_invalid_provider_json():
    client = ItemAnalysisClient(
        api_url="https://ai.example.test",
        api_key="key",
        http_client=FakeHttp({"choices": [{"message": {"content": "not json"}}]}),
    )
    try:
        client.screen(_envelope())
    except ValueError as exc:
        assert "invalid JSON" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("invalid provider JSON must be rejected")


def test_public_client_identity_is_stable():
    assert ItemAnalysisClient is CanonicalItemAnalysisClient
