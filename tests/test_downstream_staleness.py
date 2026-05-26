from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.ai.verify_client import AIVerifyResponse
from app.jobs.ai_verify_job import run_ai_verify_job
from app.jobs.claim_verify_job import run_claim_verify_job
from app.jobs.recommendation_write_job import run_recommendation_write_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIReviewItem,
    CandidateItem,
    ClaimVerificationItem,
    EvidenceItem,
    ExtractedClaim,
    NormalizedItem,
    RawItem,
    RecommendationCard,
    Source,
    VerificationItem,
)


class StaticVerifyClient:
    is_configured = True
    model = "verify-model"

    def __init__(self):
        self.calls = []

    def verify(self, request):
        self.calls.append(request)
        return AIVerifyResponse(
            verified=True,
            final_keep=True,
            final_score=99,
            recommendation_level="S",
            relevance_score=95,
            usefulness_score=95,
            credibility_score=92,
            novelty_score=90,
            reproducibility_score=90,
            audience_fit_score=90,
            source_quality_score=90,
            spam_risk_score=5,
            category="ai_tool",
            summary_cn=f"verified call {len(self.calls)}",
            recommendation_reason="AI optimistic",
            risk_reason=None,
            evidence_summary=["evidence summary"],
            risk_flags=[],
            raw_response={"call": len(self.calls)},
        )


def test_claim_verify_recomputes_when_evidence_is_newer_without_duplicate_rows(tmp_path):
    session_factory = _seed_pipeline_candidate(tmp_path / "claim_stale.db")

    first = run_claim_verify_job(session_factory=session_factory, limit=10)
    assert first.processed_claims == 1

    with session_factory() as session:
        first_row = session.query(ClaimVerificationItem).one()
        first_id = first_row.id
        evidence = session.query(EvidenceItem).one()
        evidence.supports_claim = "contradict"
        evidence.evidence_confidence = 95
        evidence.risk_flags = '["broken_primary_link"]'
        evidence.updated_at = _future()
        session.commit()

    second = run_claim_verify_job(session_factory=session_factory, limit=10)

    assert second.processed_claims == 1
    with session_factory() as session:
        rows = session.query(ClaimVerificationItem).all()
        assert len(rows) == 1
        assert rows[0].id == first_id
        assert rows[0].supports_claim == "contradict"
        assert rows[0].support_strength == "none"
        assert rows[0].source_evidence_updated_at is not None
        assert rows[0].stale is False


def test_ai_verify_recomputes_when_claim_verification_is_newer_without_duplicate_rows(tmp_path):
    session_factory = _seed_pipeline_candidate(tmp_path / "ai_stale.db")
    run_claim_verify_job(session_factory=session_factory, limit=10)
    client = StaticVerifyClient()

    first = run_ai_verify_job(session_factory=session_factory, client=client, limit=10)
    assert first.processed == 1

    with session_factory() as session:
        first_verification = session.query(VerificationItem).one()
        first_id = first_verification.id
        claim_verification = session.query(ClaimVerificationItem).one()
        claim_verification.supports_claim = "contradict"
        claim_verification.support_strength = "none"
        claim_verification.risk_flags = '["manual_contradiction"]'
        claim_verification.updated_at = _future()
        session.commit()

    second = run_ai_verify_job(session_factory=session_factory, client=client, limit=10)

    assert second.processed == 1
    with session_factory() as session:
        rows = session.query(VerificationItem).all()
        assert len(rows) == 1
        assert rows[0].id == first_id
        assert rows[0].final_keep is False
        assert rows[0].final_score <= 59
        assert "contradicted_claim" in json.loads(rows[0].risk_flags)
        assert rows[0].source_claim_verification_updated_at is not None
        assert rows[0].stale is False


def test_recommendation_write_recomputes_when_verification_is_newer_without_duplicate_cards(tmp_path):
    session_factory = _seed_pipeline_candidate(tmp_path / "card_stale.db")
    client = StaticVerifyClient()
    run_claim_verify_job(session_factory=session_factory, limit=10)
    run_ai_verify_job(session_factory=session_factory, client=client, limit=10)

    first = run_recommendation_write_job(session_factory=session_factory, limit=10)
    assert first.processed == 1

    with session_factory() as session:
        first_card = session.query(RecommendationCard).one()
        first_id = first_card.id
        verification = session.query(VerificationItem).one()
        verification.summary_cn = "更新后的可信摘要"
        verification.updated_at = _future()
        session.commit()

    second = run_recommendation_write_job(session_factory=session_factory, limit=10)

    assert second.processed == 1
    with session_factory() as session:
        rows = session.query(RecommendationCard).all()
        assert len(rows) == 1
        assert rows[0].id == first_id
        assert rows[0].summary_cn == "更新后的可信摘要"
        assert rows[0].source_verification_updated_at is not None
        assert rows[0].stale is False


def _seed_pipeline_candidate(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        source = Source(
            id="source_official",
            name="Official Source",
            type="atom",
            url="https://example.com/feed.xml",
            enabled=True,
            priority=1,
            fetch_interval=3600,
            parser_type="feedparser",
            source_group="official_blog",
            source_subtype="fixed",
        )
        session.add(source)
        session.flush()
        raw = RawItem(
            source_id=source.id,
            external_id="raw-1",
            title="ExampleProxy released",
            link="https://example.com/post",
            raw_payload="{}",
            content_hash="hash-1",
            status="normalized",
        )
        session.add(raw)
        session.flush()
        normalized = NormalizedItem(
            raw_item_id=raw.id,
            title=raw.title,
            body_text="ExampleProxy supports install usage quickstart.",
            url=raw.link,
            language="en",
            dedupe_key="dedupe-1",
        )
        session.add(normalized)
        session.flush()
        candidate = CandidateItem(
            normalized_item_id=normalized.id,
            source_group="official_blog",
            source_subtype="fixed",
            candidate_score=90,
            matched_keywords='["ai"]',
            status="kept",
        )
        session.add(candidate)
        session.flush()
        session.add(
            AIReviewItem(
                candidate_item_id=candidate.id,
                model="review-model",
                ai_keep=True,
                ai_score=90,
                category="ai_tool",
                reason="good",
                summary_cn="review summary",
                raw_response="{}",
            )
        )
        claim = ExtractedClaim(
            candidate_item_id=candidate.id,
            model="claim-model",
            entity_name="ExampleProxy",
            entity_type="ai_tool",
            official_url="https://example.com",
            claims_json='["提供安装方式"]',
            release_signal=True,
            actionable_signal=True,
            confidence=90,
            raw_response="{}",
            evidence_status="completed",
        )
        session.add(claim)
        session.flush()
        session.add(
            EvidenceItem(
                candidate_item_id=candidate.id,
                evidence_type="official_page",
                url="https://example.com/docs",
                title="ExampleProxy docs",
                snippet="install usage quickstart",
                source_domain="example.com",
                supports_claim="support",
                confidence=90,
                retrieval_score=90,
                evidence_confidence=90,
                fetched_title="ExampleProxy docs",
                fetched_description="install usage quickstart",
                fetched_text_preview="install usage quickstart setup configuration",
                raw_payload="{}",
                fetch_status="completed",
                classify_status="completed",
                classification_version="rules_v1",
                classified_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return session_factory


def _future():
    return datetime.now(timezone.utc) + timedelta(minutes=5)
