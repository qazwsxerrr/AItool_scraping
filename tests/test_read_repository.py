from __future__ import annotations

import json
from datetime import datetime, timezone

from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, IntelItemVerification, IntelRun, Source
from app.storage.read_repository import AllItemFilters, UIReadRepository


def test_read_repository_returns_empty_ui_state(tmp_path):
    session_factory = _make_session_factory(tmp_path / "empty.db")
    with session_factory() as session:
        repo = UIReadRepository(session)
        stats = repo.get_dashboard_stats()
        assert stats.raw_items == 0
        assert stats.recommendations == 0
        assert repo.list_featured_cards() == []
        assert repo.list_all_items() == []


def test_read_repository_maps_v2_items_to_existing_ui_dtos(tmp_path):
    session_factory = _make_session_factory(tmp_path / "ui.db")
    published_at = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
    with session_factory() as session:
        source = Source(
            id="github_releases",
            name="GitHub Releases",
            transport="feed",
            url="https://example.com/feed.xml",
            feed_format="rss",
            feed_adapter="generic",
            source_group="official_model_company",
            source_subtype="official",
            source_role="official",
            content_class="official_model_company",
        )
        item = IntelItem(
            source_id=source.id,
            external_id="release-1",
            title="Claude Code v2 发布",
            canonical_url="https://example.com/claude-code",
            published_at=published_at,
            summary="Claude Code release summary",
            content_text="Claude Code supports MCP reconnect.",
            content_class="official_model_company",
            metrics_json=json.dumps({"stars": 100}),
            raw_payload_json="{}",
            content_hash="hash-ui-1",
            selection_score=88,
            status="verified",
        )
        session.add_all(
            [
                source,
                item,
                AIItemReview(
                    item=item,
                    model="review-model",
                    keep=True,
                    content_class="official_model_company",
                    confidence=92,
                    reason="official release",
                    summary_cn="Claude Code 发布了一次实用更新。",
                    raw_response_json="{}",
                ),
                IntelItemVerification(
                    item=item,
                    mode="official_direct_link",
                    status="verified",
                    verification_url="https://example.com/claude-code",
                    source_domain="example.com",
                    http_status=200,
                    title="Claude Code v2 发布",
                    supports_basic_fact=True,
                    risk_flags_json="[]",
                ),
                IntelRun(status="completed"),
            ]
        )
        session.commit()

    with session_factory() as session:
        repo = UIReadRepository(session)
        cards = repo.list_featured_cards(limit=10)
        direct_cards = repo.list_featured_cards(direct_support_only=True, limit=10)
        items = repo.list_all_items(filters=AllItemFilters(query="Claude"))
        results = repo.search_content("Claude")

    assert len(cards) == 1
    assert cards[0].title == "Claude Code v2 发布"
    assert cards[0].total_score == 88
    assert cards[0].direct_support_count == 1
    assert len(direct_cards) == 1
    assert len(items) == 1
    assert items[0].source_name == "GitHub Releases"
    assert items[0].ai_keep is True
    assert results.recommendations[0].title == "Claude Code v2 发布"
    assert results.claims == []
    assert results.evidence == []


def _make_session_factory(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    return create_session_factory(engine)
