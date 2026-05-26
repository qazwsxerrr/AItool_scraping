from __future__ import annotations

from app.pipeline.claim_verification import verify_claims_for_extracted_claim
from app.storage.models import CandidateItem, EvidenceItem, ExtractedClaim


def test_claim_verification_marks_entity_only_when_repo_exists_but_specific_claim_is_not_supported():
    claim = _claim_with_evidence(
        claim_text="支持 OpenAI-compatible API",
        evidence_text="ExampleProxy repository README quickstart and install guide.",
        evidence_type="github_repo",
    )

    [decision] = verify_claims_for_extracted_claim(claim)

    assert decision.supports_claim == "neutral"
    assert decision.support_strength == "entity_only"
    assert "entity_only_support" in decision.risk_flags


def test_claim_verification_marks_direct_when_specific_claim_terms_are_in_evidence():
    claim = _claim_with_evidence(
        claim_text="支持 OpenAI-compatible API",
        evidence_text="OpenAI-compatible API. Use /v1/chat/completions with base_url and api_key.",
        evidence_type="github_repo",
    )

    [decision] = verify_claims_for_extracted_claim(claim)

    assert decision.supports_claim == "support"
    assert decision.support_strength == "direct"
    assert decision.confidence >= 80
    assert "entity_only_support" not in decision.risk_flags


def test_claim_verification_requires_weight_files_for_open_weights_claim():
    claim = _claim_with_evidence(
        claim_text="发布 open weights 模型权重",
        evidence_text="Model card exists and describes the architecture, but no files are shown.",
        evidence_type="huggingface_model",
    )

    [decision] = verify_claims_for_extracted_claim(claim)

    assert decision.supports_claim == "neutral"
    assert decision.support_strength == "entity_only"


def _claim_with_evidence(*, claim_text: str, evidence_text: str, evidence_type: str) -> ExtractedClaim:
    candidate = CandidateItem(
        id=1,
        normalized_item_id=1,
        source_group="x",
        source_subtype="account",
        candidate_score=88,
        matched_keywords='["ai"]',
        status="kept",
    )
    claim = ExtractedClaim(
        id=1,
        candidate_item_id=1,
        candidate_item=candidate,
        entity_name="ExampleProxy",
        entity_type="ai_tool",
        claims_json=f'["{claim_text}"]',
        release_signal=True,
        actionable_signal=True,
        confidence=85,
        raw_response="{}",
    )
    evidence = EvidenceItem(
        id=10,
        candidate_item_id=1,
        candidate_item=candidate,
        evidence_type=evidence_type,
        url="https://github.com/example/proxy" if evidence_type == "github_repo" else "https://huggingface.co/example/model",
        title="ExampleProxy",
        snippet=evidence_text,
        source_domain="github.com" if evidence_type == "github_repo" else "huggingface.co",
        supports_claim="support",
        confidence=88,
        retrieval_score=90,
        evidence_confidence=88,
        fetched_title="ExampleProxy",
        fetched_description=evidence_text,
        fetched_text_preview=evidence_text,
        raw_payload="{}",
        fetch_status="completed",
        classify_status="completed",
    )
    candidate.extracted_claim = claim
    candidate.evidence_items = [evidence]
    return claim
