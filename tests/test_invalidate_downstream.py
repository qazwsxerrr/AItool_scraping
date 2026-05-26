from __future__ import annotations

from app.jobs.invalidate_downstream_job import run_invalidate_downstream_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import CandidateItem, ClaimVerificationItem, ExtractedClaim, RecommendationCard, VerificationItem


def test_invalidate_from_evidence_marks_all_downstream_rows_stale(tmp_path):
    session_factory = _seed_downstream_rows(tmp_path / "invalidate_evidence.db")

    result = run_invalidate_downstream_job(session_factory=session_factory, from_stage="evidence")

    assert result.claim_verifications == 1
    assert result.verification_items == 1
    assert result.recommendation_cards == 1
    with session_factory() as session:
        assert session.query(ClaimVerificationItem).one().stale is True
        assert session.query(VerificationItem).one().stale is True
        assert session.query(RecommendationCard).one().stale is True


def test_invalidate_from_verification_only_marks_recommendation_cards_stale(tmp_path):
    session_factory = _seed_downstream_rows(tmp_path / "invalidate_verification.db")

    result = run_invalidate_downstream_job(session_factory=session_factory, from_stage="verification")

    assert result.claim_verifications == 0
    assert result.verification_items == 0
    assert result.recommendation_cards == 1
    with session_factory() as session:
        assert session.query(ClaimVerificationItem).one().stale is False
        assert session.query(VerificationItem).one().stale is False
        assert session.query(RecommendationCard).one().stale is True


def _seed_downstream_rows(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        candidate = CandidateItem(
            normalized_item_id=1,
            source_group="x",
            source_subtype="account",
            candidate_score=90,
            matched_keywords="[]",
            status="kept",
        )
        session.add(candidate)
        session.flush()
        claim = ExtractedClaim(
            candidate_item_id=candidate.id,
            claims_json='["提供安装方式"]',
            release_signal=True,
            actionable_signal=True,
            confidence=85,
            raw_response="{}",
        )
        session.add(claim)
        session.flush()
        session.add(
            ClaimVerificationItem(
                candidate_item_id=candidate.id,
                extracted_claim_id=claim.id,
                claim_index=0,
                claim_text="提供安装方式",
                supports_claim="support",
                support_strength="direct",
                evidence_item_ids_json="[]",
                confidence=88,
                risk_flags="[]",
                raw_response="{}",
                stale=False,
            )
        )
        verification = VerificationItem(
            candidate_item_id=candidate.id,
            model="verify-model",
            verified=True,
            final_keep=True,
            final_score=83,
            freshness_score=70,
            recommendation_level="A",
            relevance_score=90,
            usefulness_score=85,
            credibility_score=82,
            novelty_score=80,
            reproducibility_score=75,
            audience_fit_score=90,
            source_quality_score=70,
            spam_risk_score=15,
            category="mcp",
            summary_cn="summary",
            recommendation_reason="reason",
            risk_reason=None,
            evidence_summary="[]",
            risk_flags="[]",
            raw_response="{}",
            stale=False,
        )
        session.add(verification)
        session.flush()
        session.add(
            RecommendationCard(
                verification_item_id=verification.id,
                title="Example card",
                raw_response="{}",
                stale=False,
            )
        )
        session.commit()
    return session_factory
