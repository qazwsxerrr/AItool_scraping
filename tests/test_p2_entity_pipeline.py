from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config.source_registry import SourceConfig
from app.jobs.entity_resolve_job import run_entity_resolve_job
from app.jobs.pipeline_run_job import run_daily_job
from app.jobs.recommendation_export_job import run_recommendation_export_job
from app.pipeline.source_quality import source_quality_for_source
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIReviewItem,
    CandidateItem,
    CanonicalEntity,
    EntityMention,
    ExtractedClaim,
    NormalizedItem,
    PipelineRun,
    RawItem,
    Source,
    VerificationItem,
)
from app.storage.repository import SourceRepository


def test_source_quality_is_persisted_and_overrides_group_default(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'source_quality.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        SourceRepository(session).upsert_source(
            SourceConfig(
                id="custom_forum",
                name="Custom Forum",
                type="rss",
                url="https://example.com/rss.xml",
                source_group="linux_do",
                source_subtype="search",
                quality_weight=0.72,
                source_role="forum",
                spam_risk="low",
                requires_verification=False,
            )
        )
        session.commit()

    with session_factory() as session:
        source = session.get(Source, "custom_forum")
        assert source.source_group == "linux_do"
        assert source.source_subtype == "search"
        assert source.quality_weight == 0.72
        assert source.source_role == "forum"
        assert source.spam_risk == "low"
        assert source.requires_verification is False
        quality = source_quality_for_source(source, fallback_group="reddit_local_llama")
        assert quality["quality_weight"] == 0.72
        assert quality["requires_verification"] is False


def test_entity_resolve_merges_same_github_repo_and_creates_mentions(tmp_path):
    session_factory = _seed_verified_candidates(tmp_path / "entities.db")

    result = run_entity_resolve_job(session_factory=session_factory, limit=10)

    assert result.processed == 2
    assert result.entities_created == 1
    assert result.mentions_created == 2

    with session_factory() as session:
        entity = session.query(CanonicalEntity).one()
        assert entity.name == "Example MCP"
        assert entity.github_url == "https://github.com/example/mcp"
        assert entity.best_score == 91
        assert entity.source_count == 2
        assert entity.mention_count == 2
        assert session.query(EntityMention).count() == 2


def test_recommendation_export_deduplicates_by_resolved_entity(tmp_path):
    session_factory = _seed_verified_candidates(tmp_path / "entity_export.db")
    run_entity_resolve_job(session_factory=session_factory, limit=10)

    result = run_recommendation_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "out",
        limit=10,
    )

    assert result.exported == 1
    payload = json.loads(result.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["entity_name"] == "Example MCP"
    assert payload["mention_count"] == 2


def test_run_daily_records_pipeline_run_status_and_stats(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'pipeline.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    calls: list[str] = []

    def ok_step():
        calls.append("ok")
        return {"processed": 2, "inserted": 1}

    result = run_daily_job(session_factory=session_factory, steps=[("ok_step", ok_step)])

    assert result.status == "completed"
    assert calls == ["ok"]
    with session_factory() as session:
        run = session.query(PipelineRun).one()
        assert run.run_type == "daily"
        assert run.status == "completed"
        assert json.loads(run.stats_json)["ok_step"]["inserted"] == 1
        assert run.finished_at is not None


def _seed_verified_candidates(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        _seed_verified_candidate(
            session,
            source_id="reddit_local_llama_search_mcp",
            source_group="reddit_local_llama",
            raw_id_suffix="reddit",
            score=91,
        )
        _seed_verified_candidate(
            session,
            source_id="producthunt_feed",
            source_group="producthunt",
            raw_id_suffix="ph",
            score=83,
        )
        session.commit()
    return session_factory


def _seed_verified_candidate(session, *, source_id: str, source_group: str, raw_id_suffix: str, score: int) -> None:
    source = Source(
        id=source_id,
        name=source_id,
        type="rss",
        url=f"https://example.com/{source_id}.xml",
        source_group=source_group,
        source_subtype="fixed",
        quality_weight=0.9 if source_group == "reddit_local_llama" else 0.6,
        source_role="community",
        spam_risk="medium",
        requires_verification=True,
    )
    session.add(source)
    raw = RawItem(
        source_id=source_id,
        external_id=raw_id_suffix,
        title=f"Example MCP launch {raw_id_suffix}",
        link=f"https://example.com/{raw_id_suffix}",
        author="author",
        published_at=datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc),
        raw_summary="summary",
        raw_content="content",
        raw_payload="{}",
        content_hash=f"hash-{raw_id_suffix}",
        status="normalized",
    )
    session.add(raw)
    session.flush()
    normalized = NormalizedItem(
        raw_item_id=raw.id,
        title=raw.title,
        body_text="Example MCP server",
        url=raw.link,
        published_at=raw.published_at,
        language="en",
        dedupe_key=f"url:{raw.link}",
    )
    session.add(normalized)
    session.flush()
    candidate = CandidateItem(
        normalized_item_id=normalized.id,
        source_group=source_group,
        source_subtype="fixed",
        candidate_score=90,
        matched_keywords='["mcp"]',
        status="kept",
    )
    session.add(candidate)
    session.flush()
    session.add(
        AIReviewItem(
            candidate_item_id=candidate.id,
            model="review",
            ai_keep=True,
            ai_score=90,
            category="mcp",
            raw_response="{}",
        )
    )
    session.add(
        ExtractedClaim(
            candidate_item_id=candidate.id,
            model="claim",
            entity_name="Example MCP",
            entity_type="mcp",
            official_url="https://example.ai",
            github_url="https://github.com/example/mcp",
            claims_json='["发布了 MCP server"]',
            release_signal=True,
            actionable_signal=True,
            confidence=90,
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
            recommendation_level="S" if score >= 90 else "A",
            relevance_score=90,
            usefulness_score=90,
            credibility_score=88,
            novelty_score=80,
            reproducibility_score=80,
            audience_fit_score=90,
            source_quality_score=80,
            spam_risk_score=10,
            category="mcp",
            summary_cn="Example MCP 摘要",
            recommendation_reason="多来源提及",
            evidence_summary="[]",
            risk_flags="[]",
            raw_response="{}",
        )
    )
