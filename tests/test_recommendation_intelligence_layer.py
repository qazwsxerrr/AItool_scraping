from __future__ import annotations

import json
from datetime import datetime, timezone

from app.ai.verify_client import AIVerifyResponse
from app.config.source_registry import SourceConfig
from app.jobs.ai_verify_job import run_ai_verify_job
from app.jobs.claim_extract_job import run_claim_extract_job
from app.jobs.claim_verify_job import run_claim_verify_job
from app.jobs.entity_resolve_job import run_entity_resolve_job
from app.jobs.feedback_job import add_feedback
from app.jobs.recommendation_export_job import run_recommendation_export_job
from app.jobs.recommendation_write_job import run_recommendation_write_job
from app.parsers.feed_parser import ParsedFeedItem
from app.pipeline.normalize import normalize_raw_item
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIReviewItem,
    CandidateItem,
    CanonicalEntity,
    ClaimVerificationItem,
    ExtractedClaim,
    RawItem,
    RecommendationCard,
    VerificationItem,
)
from app.storage.repository import EvidenceItemRepository, NormalizedItemRepository, RawItemRepository, SourceRepository


class CaptureVerifyClient:
    is_configured = True
    model = "verify-model"

    def __init__(self):
        self.calls = []

    def verify(self, request):
        self.calls.append(request)
        return AIVerifyResponse(
            verified=True,
            final_keep=True,
            final_score=1,
            recommendation_level="D",
            relevance_score=90,
            usefulness_score=85,
            credibility_score=84,
            novelty_score=82,
            reproducibility_score=76,
            audience_fit_score=88,
            source_quality_score=75,
            spam_risk_score=12,
            category="mcp",
            summary_cn="Example MCP 是一个近期发布的 MCP server。",
            recommendation_reason="有 claim 级支持证据和可执行安装线索。",
            risk_reason="仍需实际试用。",
            evidence_summary=["GitHub README 支持发布与安装 claim"],
            risk_flags=[],
            raw_response={"ok": True},
        )


def test_claim_verify_creates_per_claim_records_and_ai_verify_uses_them(tmp_path):
    session_factory = _seed_candidate_with_classified_evidence(tmp_path / "claim_verify.db")

    claim_result = run_claim_verify_job(session_factory=session_factory, limit=10)

    assert claim_result.processed_claims == 1
    assert claim_result.inserted == 2
    with session_factory() as session:
        rows = session.query(ClaimVerificationItem).order_by(ClaimVerificationItem.claim_index).all()
        assert [row.claim_text for row in rows] == ["发布了 MCP server", "提供安装方式"]
        assert all(row.supports_claim == "support" for row in rows)
        assert all(row.confidence >= 80 for row in rows)
        assert all(json.loads(row.evidence_item_ids_json) for row in rows)

    client = CaptureVerifyClient()
    verify_result = run_ai_verify_job(session_factory=session_factory, client=client, limit=10)

    assert verify_result.inserted == 1
    assert client.calls[0].claim_verifications
    assert client.calls[0].claim_verifications[0]["supports_claim"] == "support"
    with session_factory() as session:
        verification = session.query(VerificationItem).one()
        assert verification.freshness_score >= 80


def test_entity_update_cards_and_feedback_rerank_are_exported(tmp_path):
    session_factory = _seed_two_verified_entities(tmp_path / "recommendation_layer.db")

    entity_result = run_entity_resolve_job(session_factory=session_factory, limit=10)
    card_result = run_recommendation_write_job(session_factory=session_factory, limit=10)

    assert entity_result.entities_created == 2
    assert card_result.inserted == 2
    with session_factory() as session:
        alpha = session.query(CanonicalEntity).filter_by(name="Alpha Tool").one()
        beta = session.query(CanonicalEntity).filter_by(name="Beta Tool").one()
        assert alpha.major_update_detected is True
        assert "release_signal" in (alpha.last_update_reason or "")
        assert session.query(RecommendationCard).count() == 2

    add_feedback(session_factory=session_factory, entity_id=alpha.id, action="hide", reason="too generic")
    add_feedback(session_factory=session_factory, entity_id=beta.id, action="like", reason="useful")
    add_feedback(session_factory=session_factory, entity_id=beta.id, action="save", reason="try later")

    export = run_recommendation_export_job(session_factory=session_factory, output_dir=tmp_path / "out", limit=1)

    lines = export.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["entity_name"] == "Beta Tool"
    assert payload["rerank_score"] > payload["final_score"]
    assert payload["recommendation_card"]["why_recommend"]
    markdown = export.markdown_path.read_text(encoding="utf-8")
    assert "推荐排序分" in markdown
    assert "怎么试" in markdown


def _seed_candidate_with_classified_evidence(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        candidate = _create_reviewed_candidate(
            session,
            source_id="reddit_local_llama_search_mcp",
            external_id="claim-1",
            title="Example MCP server released",
            source_group="reddit_local_llama",
            published_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        )
        session.add(
            ExtractedClaim(
                candidate_item_id=candidate.id,
                model="claim",
                entity_name="Example MCP",
                entity_type="mcp",
                official_url="https://example.ai",
                github_url="https://github.com/example/mcp",
                claims_json=json.dumps(["发布了 MCP server", "提供安装方式"], ensure_ascii=False),
                release_signal=True,
                actionable_signal=True,
                confidence=90,
                raw_response="{}",
                evidence_status="completed",
            )
        )
        session.flush()
        EvidenceItemRepository(session).insert_if_new(
            candidate_item_id=candidate.id,
            url="https://github.com/example/mcp",
            evidence_type="github_repo",
            title="example/mcp",
            snippet="README install usage quickstart for Example MCP server",
            source_domain="github.com",
            supports_claim="support",
            confidence=88,
            retrieval_score=100,
            evidence_confidence=88,
            raw_payload={
                "provider": "github",
                "repo_exists": True,
                "readme_exists": True,
                "license": "MIT",
                "pushed_at": "2026-05-11T00:00:00Z",
                "quality_flags": ["readme_exists", "has_license", "recent_commit", "install_docs"],
                "risk_flags": [],
            },
        )
        session.commit()
    return session_factory


def _seed_two_verified_entities(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        _create_verified_candidate(
            session,
            entity_name="Alpha Tool",
            source_id="reddit_alpha",
            external_id="alpha",
            score=86,
            freshness_score=40,
            published_at=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        )
        _create_verified_candidate(
            session,
            entity_name="Beta Tool",
            source_id="reddit_beta",
            external_id="beta",
            score=82,
            freshness_score=85,
            published_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        )
        session.commit()
    return session_factory


def _create_verified_candidate(
    session,
    *,
    entity_name: str,
    source_id: str,
    external_id: str,
    score: int,
    freshness_score: int,
    published_at: datetime,
) -> None:
    candidate = _create_reviewed_candidate(
        session,
        source_id=source_id,
        external_id=external_id,
        title=f"{entity_name} released",
        source_group="reddit_local_llama",
        published_at=published_at,
    )
    session.add(
        ExtractedClaim(
            candidate_item_id=candidate.id,
            model="claim",
            entity_name=entity_name,
            entity_type="mcp",
            official_url=f"https://{external_id}.example.ai",
            github_url=f"https://github.com/example/{external_id}",
            claims_json=json.dumps([f"{entity_name} 发布了 MCP server"], ensure_ascii=False),
            release_signal=True,
            actionable_signal=True,
            confidence=88,
            raw_response="{}",
            evidence_status="completed",
        )
    )
    session.add(
        VerificationItem(
            candidate_item_id=candidate.id,
            model="verify",
            verified=True,
            final_keep=True,
            final_score=score,
            freshness_score=freshness_score,
            recommendation_level="A",
            relevance_score=88,
            usefulness_score=86,
            credibility_score=82,
            novelty_score=80,
            reproducibility_score=78,
            audience_fit_score=84,
            source_quality_score=75,
            spam_risk_score=12,
            category="mcp",
            summary_cn=f"{entity_name} 摘要",
            recommendation_reason=f"{entity_name} 有明确使用场景。",
            risk_reason="需要实际试用。",
            evidence_summary="[]",
            risk_flags="[]",
            raw_response="{}",
        )
    )


def _create_reviewed_candidate(
    session,
    *,
    source_id: str,
    external_id: str,
    title: str,
    source_group: str,
    published_at: datetime,
) -> CandidateItem:
    SourceRepository(session).upsert_source(
        SourceConfig(
            id=source_id,
            name=source_id,
            type="rss",
            url=f"https://example.com/{source_id}.xml",
            source_group=source_group,
            source_subtype="search",
            quality_weight=0.8,
            source_role="community",
            spam_risk="medium",
            requires_verification=True,
        )
    )
    raw_id = RawItemRepository(session).insert_if_new(
        ParsedFeedItem(
            source_id=source_id,
            external_id=external_id,
            title=title,
            link=f"https://example.com/{external_id}",
            author="author",
            published_at=published_at,
            raw_summary=f"{title} with GitHub install docs.",
            raw_content=f"{title} with GitHub install docs.",
            raw_payload={"id": external_id},
            content_hash=f"hash-{external_id}",
        )
    ).item_id
    raw_item = session.get(RawItem, raw_id)
    normalized = NormalizedItemRepository(session).insert_if_new(normalize_raw_item(raw_item))
    candidate = CandidateItem(
        normalized_item_id=normalized.item_id,
        source_group=source_group,
        source_subtype="search",
        candidate_score=88,
        matched_keywords='["mcp", "github"]',
        keep_reason="test",
        status="kept",
    )
    session.add(candidate)
    session.flush()
    session.add(
        AIReviewItem(
            candidate_item_id=candidate.id,
            model="review-model",
            ai_keep=True,
            ai_score=86,
            category="mcp",
            reason="MCP release candidate",
            summary_cn="MCP 发布候选。",
            raw_response='{"keep": true}',
        )
    )
    session.flush()
    return candidate
