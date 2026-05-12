from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config.source_registry import SourceConfig
from app.jobs.claim_extract_job import run_claim_extract_job
from app.jobs.evidence_classify_job import run_evidence_classify_job
from app.jobs.evidence_fetch_job import run_evidence_fetch_job
from app.jobs.evidence_search_job import run_evidence_search_job
from app.evidence.special_verifiers import GitHubEvidenceVerifier, HuggingFaceEvidenceVerifier
from app.parsers.feed_parser import ParsedFeedItem
from app.pipeline.normalize import normalize_raw_item
from app.search.tavily_client import TavilySearchResponse, TavilySearchResult
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import EvidenceItem, ExtractedClaim, RawItem, SearchCacheItem
from app.storage.repository import (
    EvidenceItemRepository,
    NormalizedItemRepository,
    RawItemRepository,
    SourceRepository,
)


class FakeAPIResponse:
    def __init__(self, payload, status_code=200, url="https://api.example.local"):
        self.payload = payload
        self.status_code = status_code
        self.url = url
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self.payload


class FakeAPIClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, headers):
        self.calls.append({"url": url, "headers": headers})
        return self.responses.pop(0)


class FakeClaimClient:
    is_configured = True
    model = "claim-model"

    def __init__(self, *, entity_name="Example MCP", github_url="https://github.com/example/mcp"):
        self.entity_name = entity_name
        self.github_url = github_url

    def extract(self, request):
        from app.ai.claim_client import ClaimExtractResponse

        return ClaimExtractResponse(
            entity_name=self.entity_name,
            entity_type="mcp",
            official_url="https://example.ai",
            github_url=self.github_url,
            huggingface_url=None,
            producthunt_url=None,
            main_claims=["发布了 MCP server", "提供安装方式"],
            release_signal=True,
            actionable_signal=True,
            confidence=84,
            raw_response={"entity_name": self.entity_name},
        )


class FakeTavilyClient:
    is_configured = True

    def __init__(self):
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        return TavilySearchResponse(
            query=query,
            request_id="req_cache",
            usage={"credits": 1},
            results=[
                TavilySearchResult(
                    title="Example MCP Docs",
                    url="https://docs.example.ai/mcp",
                    content="Example MCP install and usage docs.",
                    retrieval_score=88,
                    raw_payload={"score": 0.88},
                )
            ],
            raw_response={"query": query, "results": [{"url": "https://docs.example.ai/mcp"}]},
        )


class RaisingTavilyClient:
    is_configured = True

    def search(self, query):
        raise AssertionError("Tavily should not be called when fresh cache exists")


class FakeFetcher:
    def fetch(self, evidence):
        from app.evidence.fetcher import EvidenceFetchResult

        if evidence.url == "https://missing.example/tool":
            return EvidenceFetchResult(
                url=evidence.url,
                final_url=evidence.url,
                http_status=404,
                url_validation_status="unreachable",
                fetched_title="Not Found",
                fetched_description=None,
                fetched_text_preview="not found",
                raw_payload={"provider": "http"},
            )
        return EvidenceFetchResult(
            url=evidence.url,
            final_url=evidence.url,
            http_status=200,
            url_validation_status="reachable",
            fetched_title="Example MCP",
            fetched_description="Install docs",
            fetched_text_preview="Example MCP server install usage quickstart.",
            raw_payload={"provider": "http"},
        )


class FakeSpecialVerifier:
    def verify(self, evidence):
        from app.evidence.fetcher import EvidenceFetchResult

        if evidence.evidence_type == "github_repo":
            return EvidenceFetchResult(
                url=evidence.url,
                final_url=evidence.url,
                http_status=200,
                url_validation_status="reachable",
                fetched_title="example/mcp",
                fetched_description="Example MCP server",
                fetched_text_preview="README install usage quickstart",
                raw_payload={
                    "provider": "github",
                    "repo_exists": True,
                    "readme_exists": True,
                    "license": "MIT",
                    "stars": 42,
                    "quality_flags": ["readme_exists", "has_license", "install_docs"],
                    "risk_flags": [],
                },
            )
        return None


def test_evidence_fetch_updates_url_validation_and_page_preview(tmp_path):
    session_factory = _seed_candidate_with_claim_and_evidence(tmp_path / "fetch.db")

    result = run_evidence_fetch_job(
        session_factory=session_factory,
        fetcher=FakeFetcher(),
        special_verifier=FakeSpecialVerifier(),
        limit=20,
    )

    assert result.processed >= 3
    assert result.failed == 0

    with session_factory() as session:
        official = session.query(EvidenceItem).filter_by(url="https://example.ai").one()
        assert official.fetch_status == "completed"
        assert official.url_validation_status == "reachable"
        assert official.http_status == 200
        assert official.fetched_title == "Example MCP"
        assert "install usage" in official.fetched_text_preview

        github = session.query(EvidenceItem).filter_by(url="https://github.com/example/mcp").one()
        assert github.fetch_status == "completed"
        assert github.url_validation_status == "reachable"
        payload = json.loads(github.raw_payload)
        assert payload["provider"] == "github"
        assert payload["readme_exists"] is True


def test_evidence_classify_marks_support_and_broken_links(tmp_path):
    session_factory = _seed_candidate_with_claim_and_evidence(tmp_path / "classify.db")
    run_evidence_fetch_job(
        session_factory=session_factory,
        fetcher=FakeFetcher(),
        special_verifier=FakeSpecialVerifier(),
        limit=20,
    )

    result = run_evidence_classify_job(session_factory=session_factory, limit=20)

    assert result.processed >= 3
    with session_factory() as session:
        github = session.query(EvidenceItem).filter_by(url="https://github.com/example/mcp").one()
        assert github.supports_claim == "support"
        assert github.evidence_confidence >= 80
        assert "has_license" in json.loads(github.quality_flags)

        broken = session.query(EvidenceItem).filter_by(url="https://missing.example/tool").one()
        assert broken.supports_claim == "contradict"
        assert broken.evidence_confidence >= 85
        assert "broken_primary_link" in json.loads(broken.risk_flags)


def test_tavily_search_cache_reuses_fresh_query_results(tmp_path):
    session_factory = _seed_reviewed_candidate(tmp_path / "cache.db", external_id="first")
    run_claim_extract_job(session_factory=session_factory, client=FakeClaimClient(), limit=10)
    first_client = FakeTavilyClient()

    first = run_evidence_search_job(session_factory=session_factory, client=first_client, limit=10)

    assert first.processed == 1
    assert first_client.calls

    with session_factory() as session:
        assert session.query(SearchCacheItem).count() >= 1

    _seed_reviewed_candidate(tmp_path / "cache.db", external_id="second")
    run_claim_extract_job(session_factory=session_factory, client=FakeClaimClient(), limit=10)

    second = run_evidence_search_job(session_factory=session_factory, client=RaisingTavilyClient(), limit=10)

    assert second.processed == 1
    assert second.failed == 0


def test_github_special_verifier_extracts_repo_quality_signals():
    evidence = EvidenceItem(url="https://github.com/example/mcp", evidence_type="github_repo", candidate_item_id=1)
    http_client = FakeAPIClient(
        [
            FakeAPIResponse(
                {
                    "full_name": "example/mcp",
                    "description": "Example MCP server",
                    "stargazers_count": 42,
                    "forks_count": 5,
                    "open_issues_count": 1,
                    "archived": False,
                    "disabled": False,
                    "private": False,
                    "license": {"spdx_id": "MIT"},
                    "default_branch": "main",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-05-01T00:00:00Z",
                    "pushed_at": "2026-05-10T00:00:00Z",
                    "topics": ["mcp"],
                    "language": "Python",
                },
                url="https://api.github.com/repos/example/mcp",
            ),
            FakeAPIResponse(
                {"content": "IyBFeGFtcGxlIE1DUAoKaW5zdGFsbCB1c2FnZSBxdWlja3N0YXJ0"},
                url="https://api.github.com/repos/example/mcp/readme",
            ),
        ]
    )

    result = GitHubEvidenceVerifier(http_client=http_client).verify(evidence)

    assert result is not None
    assert result.url_validation_status == "reachable"
    assert result.fetched_title == "example/mcp"
    assert result.raw_payload["provider"] == "github"
    assert result.raw_payload["readme_exists"] is True
    assert "has_license" in result.raw_payload["quality_flags"]
    assert "install_docs" in result.raw_payload["quality_flags"]


def test_huggingface_special_verifier_detects_model_weights():
    evidence = EvidenceItem(
        url="https://huggingface.co/example/model",
        evidence_type="huggingface_model",
        candidate_item_id=1,
    )
    http_client = FakeAPIClient(
        [
            FakeAPIResponse(
                {
                    "id": "example/model",
                    "author": "example",
                    "pipeline_tag": "text-generation",
                    "tags": ["license:mit"],
                    "likes": 10,
                    "downloads": 100,
                    "lastModified": "2026-05-10T00:00:00Z",
                    "cardData": {"license": "mit"},
                    "siblings": [
                        {"rfilename": "model.safetensors"},
                        {"rfilename": "config.json"},
                    ],
                    "gated": False,
                },
                url="https://huggingface.co/api/models/example/model",
            )
        ]
    )

    result = HuggingFaceEvidenceVerifier(http_client=http_client).verify(evidence)

    assert result is not None
    assert result.url_validation_status == "reachable"
    assert result.raw_payload["provider"] == "huggingface"
    assert result.raw_payload["has_weights"] is True
    assert "has_weights" in result.raw_payload["quality_flags"]


def _seed_candidate_with_claim_and_evidence(db_path):
    session_factory = _seed_reviewed_candidate(db_path, external_id="evidence")
    run_claim_extract_job(session_factory=session_factory, client=FakeClaimClient(), limit=10)
    run_evidence_search_job(session_factory=session_factory, client=FakeTavilyClient(), limit=10)
    with session_factory() as session:
        claim = session.query(ExtractedClaim).one()
        EvidenceItemRepository(session).insert_if_new(
            candidate_item_id=claim.candidate_item_id,
            url="https://missing.example/tool",
            evidence_type="official_page",
            title="hallucinated_url",
            snippet=None,
            source_domain="missing.example",
            supports_claim="unknown",
            confidence=20,
            retrieval_score=100,
            evidence_confidence=20,
            raw_payload={"source": "test"},
        )
        session.commit()
    return session_factory


def _seed_reviewed_candidate(db_path, *, external_id: str):
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
                external_id=external_id,
                title=f"Example MCP server released {external_id}",
                link=f"https://reddit.example/post/{external_id}",
                author="author",
                published_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
                raw_summary="Example MCP server released with GitHub and install docs.",
                raw_content="Example MCP server released with GitHub and install docs.",
                raw_payload={"id": external_id},
                content_hash=f"hash-{external_id}",
            )
        ).item_id
        raw_item = session.get(RawItem, raw_id)
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
