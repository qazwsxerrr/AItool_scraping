from __future__ import annotations

import json
from datetime import datetime, timezone

from app.ai.claim_client import ClaimExtractResponse
from app.ai.verify_client import AIVerifyResponse
from app.config.source_registry import SourceConfig
from app.jobs.ai_verify_job import run_ai_verify_job
from app.jobs.claim_extract_job import run_claim_extract_job
from app.jobs.evidence_search_job import run_evidence_search_job
from app.jobs.recommendation_export_job import run_recommendation_export_job
from app.parsers.feed_parser import ParsedFeedItem
from app.pipeline.normalize import normalize_raw_item
from app.search.tavily_client import TavilySearchResponse, TavilySearchResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import EvidenceItem, ExtractedClaim, VerificationItem
from app.storage.repository import NormalizedItemRepository, RawItemRepository, SourceRepository


class FakeClaimClient:
    is_configured = True
    model = "claim-model"

    def __init__(self):
        self.calls = []

    def extract(self, request):
        self.calls.append(request)
        return ClaimExtractResponse(
            entity_name="Example MCP",
            entity_type="mcp",
            official_url="https://example.ai",
            github_url="https://github.com/example/mcp",
            huggingface_url=None,
            producthunt_url=None,
            main_claims=["发布了 MCP server", "提供安装方式"],
            release_signal=True,
            actionable_signal=True,
            confidence=84,
            raw_response={"entity_name": "Example MCP"},
        )


class FakeTavilyClient:
    is_configured = True

    def __init__(self):
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        return TavilySearchResponse(
            query=query,
            request_id="req_fake",
            usage={"credits": 1},
            results=[
                TavilySearchResult(
                    title="Example MCP GitHub",
                    url="https://github.com/example/mcp",
                    content="README contains MCP install instructions.",
                    confidence=92,
                    raw_payload={"score": 0.92},
                )
            ],
            raw_response={"query": query, "results": [{"url": "https://github.com/example/mcp"}]},
        )


class FakeVerifyClient:
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
            credibility_score=82,
            novelty_score=80,
            reproducibility_score=75,
            audience_fit_score=90,
            source_quality_score=70,
            spam_risk_score=15,
            category="mcp",
            summary_cn="Example MCP 是一个可安装的 MCP server。",
            recommendation_reason="有官网、GitHub 和安装说明。",
            risk_reason="社区反馈仍少。",
            evidence_summary=["官网存在", "GitHub README 包含安装方式"],
            risk_flags=[],
            raw_response={"final_score": 1},
        )


def test_claim_extract_job_inserts_claims_idempotently(tmp_path):
    session_factory = _seed_reviewed_candidate(tmp_path / "claim.db")
    client = FakeClaimClient()

    first = run_claim_extract_job(session_factory=session_factory, client=client, limit=10)
    second = run_claim_extract_job(session_factory=session_factory, client=client, limit=10)

    assert first.processed == 1
    assert first.inserted == 1
    assert first.failed == 0
    assert second.processed == 0
    assert client.calls[0].title == "Example MCP server released"

    with session_factory() as session:
        claim = session.query(ExtractedClaim).one()
        assert claim.entity_name == "Example MCP"
        assert claim.github_url == "https://github.com/example/mcp"
        assert json.loads(claim.claims_json) == ["发布了 MCP server", "提供安装方式"]


def test_evidence_search_job_uses_tavily_and_is_idempotent(tmp_path):
    session_factory = _seed_reviewed_candidate(tmp_path / "evidence.db")
    run_claim_extract_job(session_factory=session_factory, client=FakeClaimClient(), limit=10)
    client = FakeTavilyClient()

    first = run_evidence_search_job(session_factory=session_factory, client=client, limit=10)
    second = run_evidence_search_job(session_factory=session_factory, client=client, limit=10)

    assert first.processed == 1
    assert first.inserted >= 3
    assert first.failed == 0
    assert second.processed == 0
    assert any("Example MCP github" in query for query in client.calls)

    with session_factory() as session:
        evidence = session.query(EvidenceItem).order_by(EvidenceItem.id).all()
        urls = {item.url for item in evidence}
        assert "https://example.ai" in urls
        assert "https://github.com/example/mcp" in urls
        github = [item for item in evidence if item.url == "https://github.com/example/mcp"][0]
        assert github.evidence_type == "github_repo"
        assert github.source_domain == "github.com"


def test_ai_verify_job_inserts_final_verification_idempotently(tmp_path):
    session_factory = _seed_reviewed_candidate(tmp_path / "verify.db")
    run_claim_extract_job(session_factory=session_factory, client=FakeClaimClient(), limit=10)
    run_evidence_search_job(session_factory=session_factory, client=FakeTavilyClient(), limit=10)
    client = FakeVerifyClient()

    first = run_ai_verify_job(session_factory=session_factory, client=client, limit=10)
    second = run_ai_verify_job(session_factory=session_factory, client=client, limit=10)

    assert first.processed == 1
    assert first.inserted == 1
    assert first.failed == 0
    assert second.processed == 0
    assert client.calls[0].candidate["title"] == "Example MCP server released"
    assert client.calls[0].evidence_items

    with session_factory() as session:
        verification = session.query(VerificationItem).one()
        assert verification.final_keep is True
        assert verification.final_score == 83
        assert verification.recommendation_level == "A"
        assert json.loads(verification.risk_flags) == []


def test_recommendation_export_writes_ranked_markdown_and_jsonl(tmp_path):
    session_factory = _seed_reviewed_candidate(tmp_path / "export.db")
    run_claim_extract_job(session_factory=session_factory, client=FakeClaimClient(), limit=10)
    run_evidence_search_job(session_factory=session_factory, client=FakeTavilyClient(), limit=10)
    run_ai_verify_job(session_factory=session_factory, client=FakeVerifyClient(), limit=10)

    result = run_recommendation_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "out",
        limit=10,
    )

    assert result.exported == 1
    markdown = result.markdown_path.read_text(encoding="utf-8")
    jsonl = result.jsonl_path.read_text(encoding="utf-8")
    assert "今日强推荐" in markdown
    assert "Example MCP server released" in markdown
    assert "final_score" in jsonl
    assert json.loads(jsonl.splitlines()[0])["recommendation_level"] == "A"


def _seed_reviewed_candidate(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        source = SourceConfig(
            id="reddit_local_llama_search_mcp",
            name="Reddit Search MCP",
            type="atom",
            url="https://example.com/feed.rss",
            enabled=True,
            priority=10,
            fetch_interval=3600,
            parser_type="feedparser",
            source_group="reddit_local_llama",
            source_subtype="search",
            default_limit=10,
        )
        SourceRepository(session).upsert_source(source)
        raw_repo = RawItemRepository(session)
        normalized_repo = NormalizedItemRepository(session)
        raw_id = raw_repo.insert_if_new(
            ParsedFeedItem(
                source_id=source.id,
                external_id="item-1",
                title="Example MCP server released",
                link="https://reddit.example/post/1",
                author="author",
                published_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
                raw_summary="Example MCP server released with GitHub and install docs.",
                raw_content="Example MCP server released with GitHub and install docs.",
                raw_payload={"id": "item-1"},
                content_hash="hash-item-1",
            )
        ).item_id
        raw_item = session.get(__import__("app.storage.models", fromlist=["RawItem"]).RawItem, raw_id)
        normalized = normalized_repo.insert_if_new(normalize_raw_item(raw_item))
        raw_repo.mark_status(raw_id, "normalized")

        from app.storage.models import AIReviewItem, CandidateItem

        candidate = CandidateItem(
            normalized_item_id=normalized.item_id,
            source_group="reddit_local_llama",
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
        session.commit()
    return session_factory
