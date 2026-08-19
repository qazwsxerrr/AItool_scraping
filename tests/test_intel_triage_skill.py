from __future__ import annotations

import json

from app.ai.skills.intel_triage import (
    AnalysisResult,
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
        "topic": "project",
        "topics": ["project"],
        "summary_cn": "一个可复用的 MCP 服务项目",
        "keywords": ["MCP", "开源"],
        "entities": [{"name": "MCP", "type": "technology", "aliases": []}],
        "selection_score": 87,
        "score_components": {
            "relevance": 90,
            "impact": 80,
            "freshness": 85,
            "source_authority": 70,
            "actionability": 80,
            "total": 87,
        },
        "paper_support": {"is_paper": False},
        "risk_flags": [],
        "reason": "项目材料明确",
        "confidence": 91,
    }
    value.update(overrides)
    return value


def test_stage_a_low_confidence_reject_becomes_uncertain():
    result = parse_screen_result(
        {"decision": "reject", "reason_code": "noise", "reason": "weak signal", "confidence": 40, "risk_flags": []},
        envelope=envelope(),
    )
    assert result.decision == "uncertain"
    assert "screen:low_confidence_reject" in result.risk_flags


def test_stage_a_only_hard_reject_reasons_survive_at_high_confidence():
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
    alias = parse_screen_result(
        {
            "decision": "reject",
            "reason_code": "ad",
            "reason": "广告营销",
            "confidence": 95,
            "risk_flags": [],
        },
        envelope=envelope(),
    )
    assert canonical.decision == "reject"
    assert alias.decision == "reject"


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


def test_stage_a_strict_parser_preserves_raw_and_threshold_compatible_fields():
    payload = {"decision": "reject", "reason_code": "ad", "reason": "marketing", "confidence": 95, "risk_flags": ["ad"]}
    result = parse_screen_result(payload, envelope=envelope())
    assert isinstance(result, ScreenResult)
    assert result.decision == "reject"
    assert result.item_id == 7
    assert result.raw_response == payload


def test_stage_b_strict_parser_has_entities_and_no_legacy_decision_fields():
    payload = analysis_payload()
    result = parse_analysis_result(payload, envelope=envelope())
    assert isinstance(result, AnalysisResult)
    assert result.selection_score == 87
    assert result.entities[0].type == "technology"
    assert not hasattr(result, "keep")
    assert not hasattr(result, "novelty")
    assert result.raw_response == payload


def test_stage_b_parser_normalizes_null_paper_source_type():
    result = parse_analysis_result(
        analysis_payload(paper_support={"is_paper": False, "source_type": None}),
        envelope=envelope(),
    )
    assert result.paper_support.source_type == "unknown"


def test_stage_b_empty_summary_falls_back_to_source_and_paper_is_risk_only():
    item = envelope(summary="来源原始摘要，包含明确的项目变化。")
    result = parse_analysis_result(
        analysis_payload(
            summary_cn="",
            topic="paper",
            topics=["paper"],
            paper_support={"is_paper": True, "supported": False},
        ),
        envelope=item,
    )
    assert result.summary_cn == item.summary
    assert "summary:fallback" in result.risk_flags
    assert "paper:unsupported" in result.risk_flags
    assert analysis_guard_failure(result) is None


def test_stage_b_empty_summary_without_source_text_remains_structural_failure():
    result = apply_analysis_guards(
        AnalysisResult.model_validate(analysis_payload(summary_cn="")),
    )
    assert result.summary_cn == ""
    assert analysis_guard_failure(result) == "summary_empty"


def test_first_party_x_accounts_are_not_marked_social_only_and_prompt_preserves_limits():
    item = envelope(
        source_content_class="official_model_company",
        source_group="x_official",
        source_role="official",
        source_subtype="account",
    )
    result = parse_analysis_result(analysis_payload(), envelope=item)
    assert result.source_content_class == "official_model_company"
    assert "source:social_only" not in result.risk_flags

    analysis_instructions = build_analysis_provider_payload(item)["instructions"]
    screen_instructions = build_screen_provider_payload(item)["instructions"]
    for instructions in (analysis_instructions, screen_instructions):
        assert "source_group=x_official" in instructions
        assert "不得补全正文未披露的交易细节" in instructions
        assert "普通 x_social、x_search" in instructions

    assert "irrelevant、spam、pure_advertisement、navigation_or_index、empty_content、duplicate_without_update" in screen_instructions
    assert "low_information、insufficient_content" in screen_instructions


def test_paper_guard_marks_arxiv_only_analysis():
    item = envelope(url="https://arxiv.org/abs/1234.5678")
    result = parse_analysis_result(
        analysis_payload(topic="paper", topics=["paper"], paper_support={"is_paper": True, "paper_url": item.url}),
        envelope=item,
    )
    assert result.paper_support.arxiv_only is True
    assert result.paper_gate_pass is False
    assert "paper:arxiv_only" in result.risk_flags


def test_payloads_are_stage_specific_and_provider_styles_are_supported():
    item = envelope()
    assert build_screen_provider_payload(item)["task"] == "intel_screen"
    assert build_analysis_provider_payload(item)["task"] == "intel_analysis"
    chat = build_screen_provider_payload(item, api_style="openai_chat")
    assert chat["response_format"]["json_schema"]["name"] == "intel_screen"
    responses = build_analysis_provider_payload(item, api_style="openai_responses")
    assert responses["text"]["format"]["name"] == "intel_analysis"
    entity_schema = responses["text"]["format"]["schema"]["properties"]["entities"]["items"]
    assert entity_schema["required"] == ["name", "type", "aliases"]


def test_client_calls_independent_stage_endpoints_and_isolates_failures():
    item = envelope()
    screen_http = FakeHttp({"decision": "pass", "reason_code": "relevant", "reason": "ok", "confidence": 90, "risk_flags": []})
    client = IntelTriageClient(api_url="https://ai.example.test", api_key="secret", http_client=screen_http)
    screen = client.screen(item)
    assert screen.decision == "pass"
    assert screen_http.calls[0]["json"]["task"] == "intel_screen"

    analysis_http = FakeHttp(analysis_payload())
    analysis_client = IntelTriageClient(api_url="https://ai.example.test", api_key="secret", http_client=analysis_http)
    analysis = analysis_client.analyze(item)
    assert analysis.selection_score == 87
    assert analysis_http.calls[0]["json"]["task"] == "intel_analysis"

    class Failing:
        def screen(self, _):
            raise RuntimeError("screen down")

        def analyze(self, _):
            raise RuntimeError("analysis down")

    screen_failed = run_screen_isolated(Failing(), [item])[0]
    analysis_failed = run_analysis_isolated(Failing(), [item])[0]
    assert screen_failed.status == "screen_failed"
    assert analysis_failed.status == "analysis_failed"
