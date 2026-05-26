from __future__ import annotations

from app.ai.verify_client import AIVerifyResponse
from app.pipeline.verification import EvidenceGuardStats, finalize_verification


def test_guard_rejects_items_without_support_evidence_even_when_ai_is_optimistic():
    final = finalize_verification(
        _optimistic_response(),
        evidence_count=3,
        guard_stats=EvidenceGuardStats(support_evidence_count=0, unknown_claim_count=2),
    )

    assert final.final_keep is False
    assert final.credibility_score <= 50
    assert final.final_score <= 65
    assert "no_support_evidence" in final.risk_flags


def test_guard_caps_high_confidence_contradictions_and_broken_primary_artifacts():
    final = finalize_verification(
        _optimistic_response(),
        evidence_count=2,
        guard_stats=EvidenceGuardStats(
            support_evidence_count=1,
            contradict_evidence_count=1,
            high_confidence_contradict_count=1,
            broken_github_count=1,
            supported_claim_count=1,
            direct_support_count=1,
        ),
    )

    assert final.final_keep is False
    assert final.final_score <= 44
    assert final.recommendation_level == "D"
    assert "high_confidence_contradiction" in final.risk_flags
    assert "broken_primary_artifact" in final.risk_flags


def test_guard_rejects_claim_level_contradiction():
    final = finalize_verification(
        _optimistic_response(),
        evidence_count=2,
        guard_stats=EvidenceGuardStats(
            support_evidence_count=1,
            supported_claim_count=1,
            contradicted_claim_count=1,
            direct_support_count=1,
        ),
    )

    assert final.final_keep is False
    assert final.final_score <= 59
    assert "contradicted_claim" in final.risk_flags


def test_guard_downgrades_entity_only_support_to_non_strong_recommendation():
    final = finalize_verification(
        _optimistic_response(),
        evidence_count=2,
        guard_stats=EvidenceGuardStats(
            support_evidence_count=1,
            supported_claim_count=0,
            neutral_claim_count=1,
            entity_only_support_count=1,
            direct_support_count=0,
        ),
    )

    assert final.final_keep is False
    assert final.credibility_score <= 60
    assert final.final_score <= 70
    assert final.recommendation_level in {"B", "C", "D"}
    assert "entity_only_support" in final.risk_flags


def _optimistic_response() -> AIVerifyResponse:
    return AIVerifyResponse(
        verified=True,
        final_keep=True,
        final_score=99,
        recommendation_level="S",
        relevance_score=95,
        usefulness_score=95,
        credibility_score=95,
        novelty_score=90,
        reproducibility_score=90,
        audience_fit_score=90,
        source_quality_score=90,
        spam_risk_score=5,
        category="ai_tool",
        summary_cn="AI 给出的高分摘要。",
        recommendation_reason="AI 认为值得推荐。",
        risk_reason=None,
        evidence_summary=["AI 声称证据充分"],
        risk_flags=[],
        raw_response={"final_score": 99},
    )
