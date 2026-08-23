from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.skills.intel_triage import (
    RawIntelEnvelope,
    build_analysis_provider_payload,
    parse_analysis_result,
)


def _payload(**overrides):
    value = {
        "topic": "product_application",
        "topics": ["product_application"],
        "summary_cn": "Acme 发布 Model X",
        "keywords": ["模型发布", "开发者可用性"],
        "entities": [],
        "b1_priority": 88,
        "score_components": {
            "audience_relevance": 88,
            "material_change": 88,
            "impact_scope": 88,
            "independent_news_value": 88,
            "specificity": 88,
        },
    }
    value.update(overrides)
    return value


def test_stage_b_minimal_projection_is_parseable():
    result = parse_analysis_result(_payload())
    assert result.summary_cn == "Acme 发布 Model X"
    assert result.keywords == ["模型发布", "开发者可用性"]
    assert result.b1_priority == 88
    assert not hasattr(result, "candidate_role")
    assert not hasattr(result, "event_signature")
    assert not hasattr(result, "material_facts")


def test_removed_stage_b_fields_are_rejected_by_strict_contract():
    for field, value in {
        "candidate_role": "event_seed",
        "role_confidence": 90,
        "role_reason_code": "seed",
        "role_reason": "has a new event",
        "event_signature": {},
        "material_facts": [],
        "paper_support": {},
        "risk_flags": [],
        "reason": "legacy reason",
        "confidence": 90,
        "source_content_class": "official_model_company",
        "source_group": "official_blog",
    }.items():
        with pytest.raises(ValidationError):
            parse_analysis_result(_payload(**{field: value}))


def test_provider_schema_contains_only_minimal_analysis_fields():
    envelope = RawIntelEnvelope(source_id="test", title="Test item", body_text="Test body")
    schema = build_analysis_provider_payload(envelope)["text"]["format"]["schema"]
    assert set(schema["required"]) == {
        "topic",
        "topics",
        "summary_cn",
        "keywords",
        "entities",
        "b1_priority",
        "score_components",
    }
    assert "candidate_role" not in schema["properties"]
    assert "role_confidence" not in schema["properties"]
    assert "role_reason_code" not in schema["properties"]
    assert "role_reason" not in schema["properties"]
    assert "event_signature" not in schema["properties"]
    assert "material_facts" not in schema["properties"]
    assert "paper_support" not in schema["properties"]
    assert "risk_flags" not in schema["properties"]
    assert "reason" not in schema["properties"]
    assert "confidence" not in schema["properties"]
    assert set(schema["properties"]["score_components"]["properties"]) == {
        "audience_relevance",
        "material_change",
        "impact_scope",
        "independent_news_value",
        "specificity",
    }
    assert schema["properties"]["keywords"]["minItems"] == 2
    assert schema["properties"]["keywords"]["maxItems"] == 4


def test_stage_b_locally_limits_keywords_to_four():
    result = parse_analysis_result(
        _payload(keywords=["核心动作", "能力变化", "开放范围", "时间节点", "背景概念"])
    )

    assert result.keywords == ["核心动作", "能力变化", "开放范围", "时间节点"]


def test_b1_score_schema_declares_the_zero_to_hundred_scale():
    envelope = RawIntelEnvelope(source_id="test", title="Test item", body_text="Test body")
    schema = build_analysis_provider_payload(envelope)["text"]["format"]["schema"]

    score_fields = [
        "audience_relevance",
        "material_change",
        "impact_scope",
        "independent_news_value",
        "specificity",
    ]
    assert "0–100" in schema["properties"]["b1_priority"]["description"]
    for field in score_fields:
        definition = schema["properties"]["score_components"]["properties"][field]
        assert definition["minimum"] == 0
        assert definition["maximum"] == 100
        assert "0–100" in definition["description"]


def test_legacy_score_dimensions_are_rejected_by_the_b1_v2_contract():
    with pytest.raises(ValueError, match="score_components is missing required fields"):
        parse_analysis_result(
            _payload(
                score_components={
                    "relevance": 88,
                    "importance": 88,
                    "impact": 88,
                    "freshness": 88,
                    "source_authority": 88,
                    "specificity": 88,
                    "tracking_value": 88,
                }
            )
        )


@pytest.mark.parametrize("legacy_field", ("selection_score", "score", "display_score", "total_score"))
def test_legacy_b1_priority_aliases_are_rejected(legacy_field):
    payload = _payload()
    payload[legacy_field] = payload.pop("b1_priority")

    with pytest.raises(ValueError, match="b1_priority"):
        parse_analysis_result(payload)


def test_redundant_score_components_total_is_rejected():
    with pytest.raises(ValidationError):
        parse_analysis_result(_payload(score_components={**_payload()["score_components"], "total": 88}))
