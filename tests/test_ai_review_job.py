from __future__ import annotations

import json
from datetime import datetime, timezone

from app.ai.review_client import AIReviewResponse
from app.config.source_registry import SourceConfig
from app.jobs.ai_review_job import run_ai_review_job
from app.jobs.prefilter_job import run_prefilter_job
from app.parsers.feed_parser import ParsedFeedItem
from app.pipeline.normalize import normalize_raw_item
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIReviewItem, CandidateItem, RawItem
from app.storage.repository import NormalizedItemRepository, RawItemRepository, SourceRepository


class FakeAIClient:
    is_configured = True

    def __init__(self):
        self.calls = []

    def review(self, request):
        self.calls.append(request)
        return AIReviewResponse(
            keep=True,
            score=87,
            category="model_release",
            reason="strong model signal",
            summary_cn="模型发布候选。",
            raw_response={"ok": True},
        )


def make_source(source_id="reddit_local_llama_new"):
    return SourceConfig(
        id=source_id,
        name=source_id,
        type="atom",
        url="https://example.com/feed.rss",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
        source_group="reddit_local_llama",
        source_subtype="fixed_new",
        default_limit=40,
    )


def make_item(source_id, external_id, title, link, content_hash, body="body"):
    return ParsedFeedItem(
        source_id=source_id,
        external_id=external_id,
        title=title,
        link=link,
        author="author",
        published_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
        raw_summary=body,
        raw_content=body,
        raw_payload={"id": external_id},
        content_hash=content_hash,
    )


def seed_one_kept_candidate(session):
    source = make_source()
    SourceRepository(session).upsert_source(source)
    raw_repo = RawItemRepository(session)
    normalized_repo = NormalizedItemRepository(session)
    raw_id = raw_repo.insert_if_new(
        make_item(
            source.id,
            "keep-1",
            "Released open weights GGUF model",
            "https://huggingface.co/example/model",
            "hash-keep",
            "New benchmark and repo release",
        )
    ).item_id
    session.flush()
    raw_item = session.get(RawItem, raw_id)
    normalized_repo.insert_if_new(normalize_raw_item(raw_item))
    raw_repo.mark_status(raw_id, "normalized")
    session.commit()


def seed_manual_candidate(session, external_id, title, score, published_at):
    source = make_source()
    SourceRepository(session).upsert_source(source)
    raw_repo = RawItemRepository(session)
    normalized_repo = NormalizedItemRepository(session)
    raw_id = raw_repo.insert_if_new(
        make_item(
            source.id,
            external_id,
            title,
            f"https://example.com/{external_id}",
            f"hash-{external_id}",
            "New AI tool workflow release",
        )
    ).item_id
    raw_item = session.get(RawItem, raw_id)
    raw_item.published_at = published_at
    normalized_result = normalized_repo.insert_if_new(normalize_raw_item(raw_item))
    candidate = CandidateItem(
        normalized_item_id=normalized_result.item_id,
        source_group="reddit_local_llama",
        source_subtype="search",
        candidate_score=score,
        matched_keywords='["workflow"]',
        keep_reason="test",
        status="kept",
    )
    session.add(candidate)
    raw_repo.mark_status(raw_id, "normalized")
    session.commit()


def test_ai_review_job_reviews_kept_candidates_and_is_idempotent(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'ai_review.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        seed_one_kept_candidate(session)
    run_prefilter_job(session_factory=session_factory, limit=10)
    client = FakeAIClient()

    first = run_ai_review_job(session_factory=session_factory, client=client, limit=10)
    second = run_ai_review_job(session_factory=session_factory, client=client, limit=10)

    assert first.processed == 1
    assert first.inserted == 1
    assert first.failed == 0
    assert second.processed == 0
    assert len(client.calls) == 1
    assert client.calls[0].title == "Released open weights GGUF model"

    with session_factory() as session:
        rows = session.query(AIReviewItem).all()
        assert len(rows) == 1
        assert rows[0].ai_keep is True
        assert rows[0].ai_score == 87
        assert rows[0].category == "model_release"
        assert json.loads(rows[0].raw_response) == {"ok": True}


def test_ai_review_job_uses_min_score_and_quality_order(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'ai_review_order.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        seed_manual_candidate(
            session,
            "low",
            "Low score but kept candidate",
            60,
            datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        )
        seed_manual_candidate(
            session,
            "newer-90",
            "Newer score 90 candidate",
            90,
            datetime(2026, 4, 28, 13, 0, tzinfo=timezone.utc),
        )
        seed_manual_candidate(
            session,
            "older-90",
            "Older score 90 candidate",
            90,
            datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc),
        )
        seed_manual_candidate(
            session,
            "top-95",
            "Top score 95 candidate",
            95,
            datetime(2026, 4, 26, 13, 0, tzinfo=timezone.utc),
        )

    client = FakeAIClient()
    result = run_ai_review_job(
        session_factory=session_factory,
        client=client,
        limit=10,
        min_candidate_score=70,
    )

    assert result.processed == 3
    assert [call.title for call in client.calls] == [
        "Top score 95 candidate",
        "Newer score 90 candidate",
        "Older score 90 candidate",
    ]
