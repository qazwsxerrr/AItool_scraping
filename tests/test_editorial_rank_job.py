from __future__ import annotations

from datetime import datetime, timezone

from app.jobs.editorial_rank_job import EditorialProfile, run_editorial_rank_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEvent, IntelEventItem, IntelEventRankingSnapshot, IntelItem, Source
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _seed(session_factory):
    with session_factory() as session:
        source = Source(id="rank_source", name="Rank source", transport="feed", url="https://rank.example", content_class="official_model_company")
        item = IntelItem(source=source, title="Model release", content_class="official_model_company", content_hash="r" * 64, status="candidate", selection_score=90, captured_at=datetime.now(timezone.utc))
        review = AIItemReview(item=item, content_class="official_model_company", topic="model", topics_json='["model"]', selection_score=90, status="success")
        session.add_all([source, item, review])
        session.flush()
        event = IntelRepository(session).upsert_event(event_key="title:model release", title="Model release", summary_cn="summary", topic="model", topics=["model"], content_class="official_model_company", display_score=90, primary_item_id=item.id, run_id=7, new_in_run_id=7)
        IntelRepository(session).upsert_event_item(event.id, item.id, source_id=source.id, source_group=source.source_group, is_primary=True)
        session.commit()
        return event.id


def test_editorial_rank_uses_only_current_new_event_ids_and_rebuilds_snapshot():
    session_factory = _db()
    event_id = _seed(session_factory)
    result = run_editorial_rank_job(session_factory=session_factory, run_id=7, event_ids=[event_id], profile=EditorialProfile(total_max=1))
    assert result.processed == 1
    assert result.selected == 1
    assert result.snapshots == 1
    result_again = run_editorial_rank_job(session_factory=session_factory, run_id=7, event_ids=[], profile=EditorialProfile(total_max=1))
    assert result_again.processed == 0
    with session_factory() as session:
        assert session.query(IntelEvent).count() == 1
        assert session.query(IntelEventRankingSnapshot).count() == 0


def test_editorial_rank_rejects_paper_without_non_arxiv_support():
    session_factory = _db()
    event_id = _seed(session_factory)
    with session_factory() as session:
        event = session.get(IntelEvent, event_id)
        event.topic = "paper"
        event.canonical_url = "https://arxiv.org/abs/1"
        session.commit()
    result = run_editorial_rank_job(session_factory=session_factory, run_id=7, event_ids=[event_id], profile=EditorialProfile(total_max=1))
    assert result.selected == 0
    assert result.rejected == 1
