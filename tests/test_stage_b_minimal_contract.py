from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ai.skills.intel_triage import RawIntelEnvelope, build_analysis_provider_payload, parse_analysis_result


def _payload(**overrides):
    value = {
        "topic": "product_application",
        "topics": ["product_application"],
        "summary_cn": "Acme 发布 Model X",
        "keywords": ["Model X"],
        "entities": [],
        "selection_score": 88,
        "score_components": {
            "relevance": 88,
            "importance": 88,
            "impact": 88,
            "freshness": 88,
            "source_authority": 88,
            "specificity": 88,
            "tracking_value": 88,
            "total": 88,
        },
    }
    value.update(overrides)
    return value


def test_stage_b_minimal_projection_is_parseable():
    result = parse_analysis_result(_payload())
    assert result.summary_cn == "Acme 发布 Model X"
    assert result.keywords == ["Model X"]
    assert result.selection_score == 88
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
    }.items():
        with pytest.raises(ValidationError):
            parse_analysis_result(_payload(**{field: value}))


def test_provider_schema_contains_only_minimal_analysis_fields():
    envelope = RawIntelEnvelope(source_id="test", title="Test item", body_text="Test body")
    schema = build_analysis_provider_payload(envelope, api_style="openai_chat")["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {
        "topic",
        "topics",
        "summary_cn",
        "keywords",
        "entities",
        "selection_score",
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
