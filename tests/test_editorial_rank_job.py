from __future__ import annotations

import json

from app.jobs.editorial_rank_job import run_editorial_rank_job
from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEvent, IntelEventItem, IntelEventRankingSnapshot, IntelItem, Source
from app.storage.read_repository import UIReadRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _add_event(session, source_id: str, event_id: str, *, topic: str, score: float, url: str | None = None):
    source = session.get(Source, source_id)
    if source is None:
        source = Source(
            id=source_id,
            name=source_id,
            transport="feed",
            url=f"https://{source_id}.example.test/feed",
            source_group=source_id,
            content_class="official_model_company",
        )
        session.add(source)
        session.flush()
    item = IntelItem(
        source_id=source_id,
        title=event_id,
        canonical_url=url,
        content_class=source.content_class,
        content_hash=(event_id + source_id).encode().hex()[:64].ljust(64, "0"),
        status="selected",
    )
    session.add(item)
    session.flush()
    event = IntelEvent(
        event_key=f"title:{event_id.casefold()}",
        normalized_title=event_id.casefold(),
        title=event_id,
        summary_cn=f"summary {event_id}",
        topic=topic,
        content_class=source.content_class,
        source_group=source.source_group,
        source_ids_json=json.dumps([source_id]),
        source_groups_json=json.dumps([source.source_group]),
        display_score=score,
        canonical_url=url,
        primary_item_id=item.id,
    )
    session.add(event)
    session.flush()
    session.add(
        IntelEventItem(
            event_id=event.id,
            item_id=item.id,
            source_id=source_id,
            source_group=source.source_group,
            is_primary=True,
        )
    )
    return event


def test_editorial_rank_uses_display_score_and_hard_caps_with_ai_fallback():
    sf = _db()
    with sf() as session:
        _add_event(session, "source_a", "high-model", topic="model", score=90)
        _add_event(session, "source_b", "low-model", topic="model", score=10)
        _add_event(session, "source_a", "paper-arxiv", topic="paper", score=99, url="https://arxiv.org/abs/1")
        session.commit()

    class BrokenRanker:
        def rank_events(self, payload):
            raise RuntimeError("provider down")

    result = run_editorial_rank_job(
        session_factory=sf,
        ai_client=BrokenRanker(),
        profile={
            "total_max": 1,
            "topic_caps": {"model": 1, "paper": 1},
            "content_class_maxima": {"official_model_company": 1},
            "source_group_maxima": {"*": 1},
            "source_id_maxima": {"*": 1},
        },
    )
    assert result.used_fallback is True
    assert result.ai_failed == 1
    assert result.selected == 1
    with sf() as session:
        snapshots = session.query(IntelEventRankingSnapshot).all()
        selected = [row for row in snapshots if row.selected]
        assert len(selected) == 1
        assert selected[0].display_score == 90
        assert selected[0].reason == "selected"
        assert any(row.reason.startswith("paper_gate") for row in snapshots if not row.selected)


def test_homepage_and_export_read_selected_ranking_snapshot(tmp_path):
    sf = _db()
    with sf() as session:
        _add_event(session, "source_a", "selected-event", topic="product", score=42, url="https://example.test/event")
        session.commit()
    run_editorial_rank_job(session_factory=sf)

    with sf() as session:
        cards = UIReadRepository(session).list_featured_cards()
        assert len(cards) == 1
        assert cards[0].title == "selected-event"
        assert cards[0].ai_status == "editorial_snapshot"

    result = run_intel_export_job(session_factory=sf, output_dir=tmp_path / "editorial-export")
    assert result.exported == 1


def test_paper_gate_reads_explicit_support_when_raw_response_omits_paper_support():
    sf = _db()
    with sf() as session:
        event = _add_event(
            session,
            "source_official_research",
            "supported-paper",
            topic="paper",
            score=90,
            url="https://research.example/papers/supported-paper",
        )
        item = session.get(IntelItem, event.primary_item_id)
        assert item is not None
        item.ai_review = AIItemReview(
            content_class="official_model_company",
            keep=True,
            status="success",
            raw_response_json="{}",
            paper_support_json=json.dumps(
                {
                    "is_paper": True,
                    "supported": True,
                    "support_level": "supported",
                    "paper_url": "https://research.example/papers/supported-paper",
                    "official_url": "https://official.example/research/supported-paper",
                }
            ),
        )
        session.commit()

    result = run_editorial_rank_job(session_factory=sf)

    assert result.selected == 1
    with sf() as session:
        snapshot = session.query(IntelEventRankingSnapshot).one()
        assert snapshot.selected is True
        assert snapshot.reason == "selected"


def test_paper_gate_accepts_persisted_evidence_url_and_official_projection():
    sf = _db()
    with sf() as session:
        event = _add_event(
            session,
            "source_official_research",
            "supported-paper-minimal-projection",
            topic="paper",
            score=90,
            url="https://research.example/papers/minimal-projection",
        )
        item = session.get(IntelItem, event.primary_item_id)
        assert item is not None
        item.ai_review = AIItemReview(
            content_class="official_model_company",
            keep=True,
            status="success",
            raw_response_json="{}",
            paper_support_json=json.dumps(
                {
                    "evidence_url": "https://official.example/research/minimal-projection",
                    "has_official_source": True,
                }
            ),
        )
        session.commit()

    result = run_editorial_rank_job(session_factory=sf)

    assert result.selected == 1
    with sf() as session:
        snapshot = session.query(IntelEventRankingSnapshot).one()
        assert snapshot.selected is True


def test_paper_gate_accepts_evidence_links_and_code_projection():
    sf = _db()
    with sf() as session:
        event = _add_event(
            session,
            "source_project",
            "supported-paper-code-projection",
            topic="paper",
            score=90,
            url="https://research.example/papers/code-projection",
        )
        item = session.get(IntelItem, event.primary_item_id)
        assert item is not None
        item.ai_review = AIItemReview(
            content_class="project_tool",
            keep=True,
            status="success",
            raw_response_json="{}",
            paper_support_json=json.dumps(
                {
                    "evidence_links": ["https://github.com/example/code-projection"],
                    "has_code": True,
                }
            ),
        )
        session.commit()

    result = run_editorial_rank_job(session_factory=sf)

    assert result.selected == 1


def test_paper_gate_rejects_explicit_arxiv_only_projection():
    sf = _db()
    with sf() as session:
        event = _add_event(
            session,
            "source_official_research",
            "arxiv-only-projection",
            topic="paper",
            score=90,
            url="https://research.example/papers/arxiv-only-projection",
        )
        item = session.get(IntelItem, event.primary_item_id)
        assert item is not None
        item.ai_review = AIItemReview(
            content_class="official_model_company",
            keep=True,
            status="success",
            raw_response_json="{}",
            paper_support_json=json.dumps(
                {
                    "is_paper": True,
                    "paper_url": "https://arxiv.org/abs/2608.99999",
                    "arxiv_only": True,
                    "evidence_url": "https://arxiv.org/abs/2608.99999",
                }
            ),
        )
        session.commit()

    result = run_editorial_rank_job(session_factory=sf)

    assert result.selected == 0
    with sf() as session:
        snapshot = session.query(IntelEventRankingSnapshot).one()
        assert snapshot.selected is False
        assert snapshot.reason == "paper_gate:arxiv_only"


def test_paper_gate_rejects_arxiv_evidence_url_without_paper_url():
    sf = _db()
    with sf() as session:
        event = _add_event(
            session,
            "source_official_research",
            "arxiv-evidence-url-only",
            topic="paper",
            score=90,
            url="https://research.example/papers/arxiv-evidence-url-only",
        )
        item = session.get(IntelItem, event.primary_item_id)
        assert item is not None
        item.ai_review = AIItemReview(
            content_class="official_model_company",
            keep=True,
            status="success",
            raw_response_json="{}",
            paper_support_json=json.dumps(
                {
                    "evidence_url": "https://arxiv.org/abs/2608.11111",
                    "supported": True,
                    "hard_gate_pass": True,
                }
            ),
        )
        session.commit()

    result = run_editorial_rank_job(session_factory=sf)

    assert result.selected == 0
    with sf() as session:
        snapshot = session.query(IntelEventRankingSnapshot).one()
        assert snapshot.selected is False
        assert snapshot.reason == "paper_gate:arxiv_only"


def test_paper_gate_rejects_arxiv_evidence_links_without_non_arxiv_support():
    sf = _db()
    with sf() as session:
        event = _add_event(
            session,
            "source_project",
            "arxiv-evidence-links-only",
            topic="paper",
            score=90,
            url="https://research.example/papers/arxiv-evidence-links-only",
        )
        item = session.get(IntelItem, event.primary_item_id)
        assert item is not None
        item.ai_review = AIItemReview(
            content_class="project_tool",
            keep=True,
            status="success",
            raw_response_json="{}",
            paper_support_json=json.dumps(
                {
                    "evidence_links": ["https://arxiv.org/abs/2608.22222"],
                    "support_level": "supported",
                }
            ),
        )
        session.commit()

    result = run_editorial_rank_job(session_factory=sf)

    assert result.selected == 0
    with sf() as session:
        snapshot = session.query(IntelEventRankingSnapshot).one()
        assert snapshot.selected is False
        assert snapshot.reason == "paper_gate:arxiv_only"
