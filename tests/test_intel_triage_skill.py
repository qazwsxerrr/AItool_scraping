from __future__ import annotations

import json

import pytest

from app.ai.skills.intel_triage import (
    AnalysisResult,
    INTEL_TOPIC_LABELS,
    INTEL_TOPICS,
    IntelTriageClient,
    RawIntelEnvelope,
    ScreenResult,
    analysis_guard_failure,
    apply_analysis_guards,
    apply_screen_guard,
    build_analysis_provider_payload,
    build_screen_provider_payload,
    parse_analysis_result,
    parse_screen_result,
    run_analysis_isolated,
    run_screen_isolated,
)


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


def envelope(**overrides):
    value = {
        "item_id": 7,
        "source_id": "feed",
        "source_content_class": "project_tool",
        "title": "MCP project",
        "url": "https://example.test/mcp",
        "body_text": "A reusable MCP server.",
    }
    value.update(overrides)
    return RawIntelEnvelope(**value)


def analysis_payload(**overrides):
    value = {
        "topic": "developer_ecosystem",
        "topics": ["developer_ecosystem"],
        "summary_cn": "一个可复用的 MCP 服务项目",
        "keywords": ["MCP", "开源"],
        "entities": [{"name": "MCP", "type": "technology", "aliases": []}],
        "b1_priority": 87,
        "score_components": {
            "audience_relevance": 90,
            "material_change": 85,
            "impact_scope": 80,
            "independent_news_value": 85,
            "specificity": 80,
        },
    }
    value.update(overrides)
    return value


def test_stage_a_low_confidence_reject_becomes_uncertain():
    result = parse_screen_result(
        {"decision": "reject", "reason_code": "spam", "reason": "weak signal", "confidence": 40, "risk_flags": []},
        envelope=envelope(),
    )
    assert result.decision == "uncertain"
    assert "screen:low_confidence_reject" in result.risk_flags


def test_stage_a_only_canonical_hard_reject_reasons_survive_at_high_confidence():
    canonical = parse_screen_result(
        {
            "decision": "reject",
            "reason_code": "irrelevant",
            "reason": "明确与 AI 情报无关",
            "confidence": 90,
            "risk_flags": [],
        },
        envelope=envelope(),
    )
    advertisement = parse_screen_result(
        {
            "decision": "reject",
            "reason_code": "pure_advertisement",
            "reason": "广告营销",
            "confidence": 95,
            "risk_flags": [],
        },
        envelope=envelope(),
    )
    assert canonical.decision == "reject"
    assert advertisement.decision == "reject"


def test_stage_a_non_hard_reject_reaches_stage_b_even_at_high_confidence():
    for reason_code in ("low_information", "insufficient_content", "source_uncertain", "unknown"):
        result = parse_screen_result(
            {
                "decision": "reject",
                "reason_code": reason_code,
                "reason": "内容仍需后续分析",
                "confidence": 99,
                "risk_flags": [],
            },
            envelope=envelope(),
        )
        assert result.decision == "uncertain"
        assert "screen:non_hard_reject_reason" in result.risk_flags


def test_stage_a_strict_parser_preserves_raw_and_canonical_fields():
    payload = {"decision": "reject", "reason_code": "pure_advertisement", "reason": "marketing", "confidence": 95, "risk_flags": ["advertisement"]}
    result = parse_screen_result(payload, envelope=envelope())
    assert isinstance(result, ScreenResult)
    assert result.decision == "reject"
    assert result.item_id == 7
    assert result.raw_response == payload


def test_stage_b_strict_parser_has_entities_and_no_legacy_decision_fields():
    payload = analysis_payload()
    result = parse_analysis_result(payload, envelope=envelope())
    assert isinstance(result, AnalysisResult)
    assert result.b1_priority == 86
    assert result.entities[0].type == "technology"
    assert not hasattr(result, "keep")
    assert not hasattr(result, "novelty")
    assert result.raw_response == payload


def test_stage_b_uses_juya_six_topic_taxonomy_only():
    assert INTEL_TOPICS == (
        "developer_ecosystem",
        "model_release",
        "product_application",
        "industry_dynamics",
        "technology_insight",
        "outlook_rumor",
    )
    assert tuple(INTEL_TOPIC_LABELS.values()) == (
        "开发生态",
        "模型发布",
        "产品应用",
        "行业动态",
        "技术与洞察",
        "前瞻与传闻",
    )
    schema = build_analysis_provider_payload(envelope())["text"]["format"]["schema"]
    assert "developer_ecosystem" in schema["properties"]["topic"]["enum"]
    with pytest.raises(ValueError, match="topic must be one of"):
        parse_analysis_result(analysis_payload(topic="paper", topics=["paper"]))


def test_stage_b_removed_contract_fields_are_not_in_schema():
    schema = build_analysis_provider_payload(envelope())["text"]["format"]["schema"]
    for field in (
        "candidate_role",
        "role_confidence",
        "role_reason_code",
        "role_reason",
        "event_signature",
        "material_facts",
        "paper_support",
        "risk_flags",
        "reason",
        "confidence",
    ):
        assert field not in schema["properties"]


def test_stage_b_priority_uses_only_the_five_content_value_components():
    result = parse_analysis_result(
        analysis_payload(
            b1_priority=0,
            score_components={
                "audience_relevance": 100,
                "material_change": 80,
                "impact_scope": 60,
                "independent_news_value": 40,
                "specificity": 20,
            },
        )
    )

    assert result.b1_priority == 73


def test_stage_b_empty_summary_falls_back_to_source():
    item = envelope(summary="来源原始摘要，包含明确的项目变化。")
    result = parse_analysis_result(
        analysis_payload(
            summary_cn="",
            topic="technology_insight",
            topics=["technology_insight"],
        ),
        envelope=item,
    )
    assert result.summary_cn == item.summary
    assert not hasattr(result, "risk_flags")
    assert analysis_guard_failure(result) is None


def test_stage_b_empty_summary_without_source_text_remains_structural_failure():
    result = apply_analysis_guards(
        AnalysisResult.model_validate(analysis_payload(summary_cn="")),
    )
    assert result.summary_cn == ""
    assert analysis_guard_failure(result) == "summary_empty"


def test_analysis_keeps_source_metadata_out_of_the_b1_result_and_prompt_is_minimal():
    item = envelope(
        source_content_class="official_model_company",
        source_group="x_official",
    )
    result = parse_analysis_result(analysis_payload(), envelope=item)
    assert not hasattr(result, "source_content_class")
    payload = build_analysis_provider_payload(item)
    assert '"source_group": "x_official"' in payload["input"][1]["content"]
    assert not hasattr(result, "risk_flags")

    analysis_instructions = payload["input"][0]["content"]
    screen_instructions = build_screen_provider_payload(item)["input"][0]["content"]
    assert "summary_cn" in analysis_instructions
    assert "keywords" in analysis_instructions
    assert "b1_priority" in analysis_instructions
    assert "AI 主体相关性" in analysis_instructions
    assert "audience_relevance=45%" in analysis_instructions
    assert "impact_scope=25%" in analysis_instructions
    assert "只返回 2–4 个" in analysis_instructions
    assert "source_authority" not in analysis_instructions
    assert "tracking_value" not in analysis_instructions
    assert "event_signature" not in analysis_instructions
    assert "paper_support" not in analysis_instructions
    assert "source_group=x_official" in screen_instructions

    assert "irrelevant、spam、pure_advertisement、navigation_or_index、empty_content、duplicate_without_update" in screen_instructions
    assert "low_information、insufficient_content" in screen_instructions


def test_stage_a_prompt_uses_source_independent_positive_scope_evidence():
    instructions = build_screen_provider_payload(envelope())["input"][0]["content"]

    assert "通过标准由三项正向证据组成" in instructions
    assert "AI 模型、推理、智能体" in instructions
    assert "所有来源使用同一内容标准" in instructions


def test_payloads_are_stage_specific_and_responses_only():
    item = envelope()
    screen = build_screen_provider_payload(item)
    assert screen["text"]["format"]["name"] == "intel_screen"
    responses = build_analysis_provider_payload(item)
    assert responses["text"]["format"]["name"] == "intel_analysis"
    entity_schema = responses["text"]["format"]["schema"]["properties"]["entities"]["items"]
    assert entity_schema["required"] == ["name", "type", "aliases"]


def test_client_calls_independent_stage_endpoints_and_isolates_failures():
    item = envelope()
    screen_http = FakeHttp({"decision": "pass", "reason_code": "relevant", "reason": "ok", "confidence": 90, "risk_flags": []})
    client = IntelTriageClient(api_url="https://ai.example.test", api_key="secret", model="test", http_client=screen_http)
    screen = client.screen(item)
    assert screen.decision == "pass"
    assert screen_http.calls[0]["url"].endswith("/responses")
    assert screen_http.calls[0]["json"]["text"]["format"]["name"] == "intel_screen"

    analysis_http = FakeHttp(analysis_payload())
    analysis_client = IntelTriageClient(api_url="https://ai.example.test", api_key="secret", model="test", http_client=analysis_http)
    analysis = analysis_client.analyze(item)
    assert analysis.b1_priority == 86
    assert analysis_http.calls[0]["json"]["text"]["format"]["name"] == "intel_analysis"

    class Failing:
        def screen(self, _):
            raise RuntimeError("screen down")

        def analyze(self, _):
            raise RuntimeError("analysis down")

    screen_failed = run_screen_isolated(Failing(), [item])[0]
    analysis_failed = run_analysis_isolated(Failing(), [item])[0]
    assert screen_failed.status == "screen_failed"
    assert analysis_failed.status == "analysis_failed"


def test_schema_failures_keep_the_full_responses_payload_for_stage_a_and_b_audit():
    item = envelope()
    screen_raw = {"id": "screen-invalid", "output_text": '{"decision":"pass"}'}
    screen_result = run_screen_isolated(
        IntelTriageClient(
            api_url="https://ai.example.test",
            api_key="secret",
            model="test",
            http_client=FakeHttp(screen_raw),
        ),
        [item],
    )[0]
    assert screen_result.status == "screen_failed"
    assert screen_result.raw_response == screen_raw

    analysis_raw = {"id": "analysis-invalid", "output_text": '{"topic":"model_release"}'}
    analysis_result = run_analysis_isolated(
        IntelTriageClient(
            api_url="https://ai.example.test",
            api_key="secret",
            model="test",
            http_client=FakeHttp(analysis_raw),
        ),
        [item],
    )[0]
    assert analysis_result.status == "analysis_failed"
    assert analysis_result.raw_response == analysis_raw
