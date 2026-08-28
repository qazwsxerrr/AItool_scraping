from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ai.skills.intel_triage import (
    IntelTriageClient,
    RawIntelEnvelope,
    strict_parse_analysis,
    strict_parse_screen,
)
from app.ai.skills.stage_d_selection import (
    STAGE_D_SELECTION_SCHEMA_VERSION,
    StageDSelectionClient,
)
from app.config.settings import Settings


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _Http:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, **kwargs):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _Response(self.payload)


def _envelope() -> RawIntelEnvelope:
    return RawIntelEnvelope(
        item_id=7,
        source_id="feed",
        title="OpenAI 发布结构化输出更新",
        url="https://example.test/news",
        body_text="更新同时适用于 Responses 和 Chat Completions。",
    )


def _response(style: str, data: dict) -> dict:
    text = json.dumps(data, ensure_ascii=False)
    if style == "responses":
        return {"id": "resp-1", "output": [{"content": [{"type": "output_text", "text": text}]}]}
    return {"id": "chat-1", "choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.mark.parametrize("style", ["responses", "chat_completions"])
def test_stage_a_protocols_produce_the_same_model_and_default_risk_flags(style):
    data = {
        "decision": "pass",
        "reason_code": "relevant",
        "reason": "AI 是主叙事且存在明确更新。",
        "confidence": 92,
    }
    http = _Http(_response(style, data))
    client = IntelTriageClient(
        api_url="https://ai.example.test/v1/responses",
        api_key="secret",
        model="test-model",
        api_style=style,
        http_client=http,
    )

    result = client.screen(_envelope())

    assert result.risk_flags == []
    assert result.model_dump(exclude={"raw_response"}) == {
        "item_id": 7,
        **data,
        "risk_flags": [],
        "status": "success",
        "error_code": None,
        "error_message": None,
    }
    request = http.calls[0]
    if style == "responses":
        assert request["url"].endswith("/responses")
        assert "input" in request["json"] and "text" in request["json"]
        assert "messages" not in request["json"] and "response_format" not in request["json"]
    else:
        assert request["url"].endswith("/chat/completions")
        assert "messages" in request["json"] and "response_format" in request["json"]
        assert "input" not in request["json"] and "text" not in request["json"]


def test_stage_a_missing_core_field_still_fails():
    with pytest.raises(ValidationError):
        strict_parse_screen(
            {
                "decision": "pass",
                "reason_code": "relevant",
                "confidence": 92,
            }
        )


def test_business_parser_preserves_the_unmodified_input_for_audit():
    data = {
        "decision": "pass",
        "reason_code": "relevant",
        "reason": "AI 是主叙事。",
        "confidence": 90,
    }

    result = strict_parse_screen(data, envelope=_envelope())

    assert result.item_id == 7
    assert result.raw_response == data


def _analysis_data() -> dict:
    return {
        "topic": "developer_ecosystem",
        "topics": ["developer_ecosystem"],
        "summary_cn": "结构化输出现在可通过两种协议接入同一业务模型。",
        "keywords": ["结构化输出", "协议适配"],
        "entities": [],
        "b1_priority": 88,
        "score_components": {
            "audience_relevance": 95,
            "material_change": 80,
            "impact_scope": 85,
            "independent_news_value": 88,
            "specificity": 90,
        },
    }


@pytest.mark.parametrize("style", ["responses", "chat_completions"])
def test_stage_b_protocols_produce_the_same_business_model(style):
    data = _analysis_data()
    client = IntelTriageClient(
        api_url="https://ai.example.test/v1/responses",
        api_key="secret",
        model="test-model",
        api_style=style,
        http_client=_Http(_response(style, data)),
    )

    result = client.analyze(_envelope())

    assert result.model_dump(exclude={"raw_response"}) == {
        "item_id": 7,
        **data,
        "b1_priority": 90,
        "status": "success",
        "error_code": None,
        "error_message": None,
    }


def test_stage_b_missing_score_component_still_fails():
    data = _analysis_data()
    del data["score_components"]["specificity"]

    with pytest.raises(ValidationError):
        strict_parse_analysis(data)


@pytest.mark.parametrize("style", ["responses", "chat_completions"])
def test_stage_d_uses_the_same_business_contract_for_both_protocols(style):
    data = {
        "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
        "selected": [{"event_id": 1, "reason_code": "daily_value", "reason": "今日价值明确。"}],
        "unselected": [{"event_id": 2, "reason_code": "lower_value", "reason": "优先级较低。"}],
    }
    http = _Http(_response(style, data))
    client = StageDSelectionClient(
        api_url="https://ai.example.test/v1/responses",
        api_key="secret",
        model="test-model",
        api_style=style,
        max_retries=0,
        http_client=http,
    )

    result = client.select(
        [{"event_id": 1, "title": "事件一"}, {"event_id": 2, "title": "事件二"}],
        max_selected=1,
    )

    assert result.parsed.model_dump(mode="json") == data
    assert result.request_metadata["transport"] == style


def test_stage_a_b_d_business_layers_do_not_parse_protocol_fields():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "app/ai/skills/intel_triage/client.py",
        root / "app/ai/skills/intel_triage/parser.py",
        root / "app/ai/skills/stage_d_selection/client.py",
        root / "app/ai/skills/stage_d_selection/parser.py",
    ]
    forbidden = ("output_text", "choices", "response_format", '"text": {"format"')

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert all(field not in source for field in forbidden), path


def test_settings_select_structured_protocol_explicitly(monkeypatch):
    monkeypatch.setenv("AI_STRUCTURED_API_STYLE", "chat_completions")
    settings = Settings.from_env(dotenv_path="/path/that/does/not/exist")
    assert settings.ai_structured_api_style == "chat_completions"

    monkeypatch.setenv("AI_STRUCTURED_API_STYLE", "auto")
    with pytest.raises(ValueError, match="responses or chat_completions"):
        Settings.from_env(dotenv_path="/path/that/does/not/exist")
