from __future__ import annotations

from datetime import datetime, timezone

from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, IntelRun, Source
from app.storage.read_repository import AllItemFilters, UIReadRepository


def _make_session_factory(db_path):
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    return create_session_factory(engine)


def test_read_repository_returns_empty_ui_state(tmp_path):
    session_factory = _make_session_factory(tmp_path / "empty.db")
    with session_factory() as session:
        repo = UIReadRepository(session)
        assert repo.get_dashboard_stats().raw_items == 0
        assert repo.list_featured_cards() == []
        assert repo.list_all_items() == []


def test_read_repository_maps_ai_items_and_source_attribution(tmp_path):
    session_factory = _make_session_factory(tmp_path / "ui.db")
    with session_factory() as session:
        source = Source(
            id="official_feed", name="Official Feed", transport="feed", url="https://example.com/feed.xml",
            feed_format="rss", feed_adapter="generic", source_group="official_blog",
            source_subtype="official", source_role="official", content_class="official_model_company",
        )
        item = IntelItem(
            source_id=source.id, external_id="release-1", title="Claude Code v2 发布",
            canonical_url="https://example.com/claude-code", published_at=datetime(2026, 8, 4, 8, tzinfo=timezone.utc),
            summary="source summary", content_text="supports MCP", content_class="official_model_company",
            content_hash="hash-ui-1", selection_score=88, status="selected",
        )
        item.ai_review = AIItemReview(
            model="review-model", status="success", keep=True, content_class="official_model_company", confidence=92,
            reason="official release", summary_cn="Claude Code 发布了一次实用更新。", raw_response_json="{}",
        )
        session.add_all([source, item, IntelRun(status="completed")])
        session.commit()

    with session_factory() as session:
        repo = UIReadRepository(session)
        cards = repo.list_featured_cards(limit=10)
        items = repo.list_all_items(filters=AllItemFilters(query="Claude"))
        results = repo.search_content("Claude")

    assert len(cards) == 1
    assert cards[0].title == "Claude Code v2 发布"
    assert cards[0].source_group == "official_blog"
    assert cards[0].ai_keep is True
    assert len(items) == 1
    assert items[0].source_name == "Official Feed"
    assert results.selected_items[0].title == "Claude Code v2 发布"
    assert not hasattr(results, "evidence")


def test_project_hotspot_and_ai_failure_statuses(tmp_path):
    session_factory = _make_session_factory(tmp_path / "status.db")
    with session_factory() as session:
        source = Source(
            id="github_trending", name="GitHub Trending", transport="github", url="https://github.com/trending",
            source_group="github_trending", source_subtype="trending", content_class="project_tool",
        )
        project = IntelItem(
            source_id=source.id, external_id="github_repo:demo/project", title="Demo project",
            canonical_url="https://github.com/demo/project", content_class="project_tool",
            content_hash="project-contract", status="hotspot",
        )
        failed = IntelItem(
            source_id=source.id, external_id="github_repo:demo/failed", title="Failed project",
            canonical_url="https://github.com/demo/failed", content_class="project_tool",
            content_hash="failed-contract", status="ai_failed",
        )
        session.add_all([source, project, failed])
        session.commit()

    with session_factory() as session:
        repo = UIReadRepository(session)
        stats = repo.get_dashboard_stats()
        cards = repo.list_featured_cards()
        options = repo.list_filter_options()

    assert stats.ai_failed_items == 1
    assert cards == []
    assert "hotspot" not in options.statuses
    assert "ai_failed" in options.statuses
