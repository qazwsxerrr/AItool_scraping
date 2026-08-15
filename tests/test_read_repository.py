from __future__ import annotations

from datetime import datetime, timezone

from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, AIItemScreen, IntelEvent, IntelEventItem, IntelEventRankingSnapshot, IntelItem, Source
from app.storage.read_repository import AllItemFilters, UIReadRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def test_read_repository_maps_snapshot_provenance_and_screen_audit():
    session_factory = _db()
    with session_factory() as session:
        source = Source(id="ui_source", name="UI source", transport="feed", url="https://ui.example", source_group="official_blog", content_class="official_model_company")
        item = IntelItem(source=source, title="Model update", canonical_url="https://ui.example/update", content_class="official_model_company", content_hash="u" * 64, status="candidate", selection_score=88, captured_at=datetime.now(timezone.utc))
        screen = AIItemScreen(item=item, decision="pass", reason_code="signal", reason="useful", confidence=93, status="success")
        review = AIItemReview(item=item, content_class="official_model_company", topic="model", topics_json='["model"]', summary_cn="摘要", selection_score=88, status="success")
        session.add_all([source, item, screen, review])
        session.flush()
        event = IntelEvent(event_key="title:model update", title="Model update", summary_cn="摘要", topic="model", content_class="official_model_company", display_score=88, new_in_run_id=3, first_seen_at=datetime.now(timezone.utc))
        session.add(event)
        session.flush()
        session.add(IntelEventItem(event=event, item=item, source=source, source_id=source.id, is_primary=True, match_type="exact"))
        session.add(IntelEventRankingSnapshot(snapshot_key="latest", event_id=event.id, run_id=3, rank=1, display_score=88, selected=True, topic="model", content_class="official_model_company"))
        session.commit()
    with session_factory() as session:
        repo = UIReadRepository(session)
        cards = repo.list_featured_events()
        items = repo.list_all_items(filters=AllItemFilters(screen_decision="pass"))
        stats = repo.get_dashboard_stats()
    assert cards and cards[0].provenance == "new"
    assert cards[0].source_refs[0]["match_type"] == "exact"
    assert items and items[0].screen_decision == "pass"
    assert stats.selected_items == 1


def test_read_repository_snapshot_empty_state_has_no_item_fallback():
    session_factory = _db()
    with session_factory() as session:
        source = Source(id="empty_source", name="Empty source", transport="feed", url="https://empty.example", content_class="official_model_company")
        session.add(source)
        session.flush()
        session.add(IntelItem(source=source, title="Candidate", content_class="official_model_company", content_hash="e" * 64, status="candidate"))
        session.commit()
    with session_factory() as session:
        assert UIReadRepository(session).list_featured_cards() == []
