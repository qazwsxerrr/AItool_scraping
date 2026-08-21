from __future__ import annotations

from datetime import datetime, timezone

from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import Source
from app.storage.read_repository import UIReadRepository
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _publish(session_factory, *, edition_date: str, title: str, event_key: str) -> None:
    with session_factory() as session:
        repo = IntelRepository(session)
        if session.get(Source, "ui-source") is None:
            session.add(
                Source(
                    id="ui-source",
                    name="UI source",
                    transport="feed",
                    url="https://ui.example/feed.xml",
                    source_group="official_blog",
                    content_class="official_model_company",
                )
            )
            session.flush()
        repo.replace_published_daily_report(
            edition_date=edition_date,
            records=[
                {
                    "event_key": event_key,
                    "title": title,
                    "original_title": "Model update",
                    "summary_cn": "摘要",
                    "url": "https://ui.example/update",
                    "display_score": 88,
                    "topic": "model_release",
                    "content_class": "official_model_company",
                    "source_group": "official_blog",
                    "source_ids": ["ui-source"],
                    "source_refs": [
                        {
                            "source_id": "ui-source",
                            "source_name": "UI source",
                            "source_group": "official_blog",
                            "source_url": "https://ui.example/update",
                            "title": "Model update",
                            "is_primary": True,
                            "match_type": "exact_url_or_external",
                            "match_confidence": 100,
                        }
                    ],
                    "risk_flags": ["needs_review"],
                    "keywords": ["model_release"],
                    "entities": [{"type": "company", "name": "Example"}],
                    "provenance": {"kind": "new"},
                    "metadata": {
                        "reason_code": "material_change",
                        "reason": "变化明确",
                    },
                }
            ],
        )
        session.commit()


def test_read_repository_maps_final_report_provenance_and_source_refs():
    session_factory = _db()
    _publish(
        session_factory,
        edition_date="2026-08-19",
        title="模型展示标题",
        event_key="url:https://ui.example/update",
    )

    with session_factory() as session:
        repo = UIReadRepository(session)
        edition = repo.resolve_edition(edition_date="2026-08-19")
        cards = repo.list_featured_events(edition=edition)
        stats = repo.get_dashboard_stats(edition=edition)
        detail = repo.get_selected_event_detail(cards[0].id, edition=edition) if cards else None

    assert cards and cards[0].provenance == "new"
    assert cards[0].source_refs[0]["match_type"] == "exact_url_or_external"
    assert cards[0].risk_flags == ["needs_review"]
    assert stats.selected_items == 1
    assert detail is not None
    assert detail.selection_reason == "变化明确"
    assert detail.resolution_method == "published_daily_report"
    assert detail.members[0].source_id == "ui-source"
    assert detail.members[0].review_topic == "model_release"


def test_read_repository_has_no_raw_item_or_snapshot_fallback():
    session_factory = _db()

    with session_factory() as session:
        reader = UIReadRepository(session)
        assert reader.resolve_edition() is None
        assert reader.list_featured_cards() == []
        assert reader.list_featured_events() == []
        assert reader.get_dashboard_stats().selected_items == 0


def test_same_date_republication_replaces_the_public_read_model():
    session_factory = _db()
    _publish(
        session_factory,
        edition_date="2026-08-18",
        title="First edition",
        event_key="url:https://ui.example/first",
    )
    _publish(
        session_factory,
        edition_date="2026-08-18",
        title="Replacement edition",
        event_key="url:https://ui.example/replacement",
    )

    with session_factory() as session:
        reader = UIReadRepository(session)
        edition = reader.resolve_edition(edition_date="2026-08-18")
        cards = reader.list_featured_cards(edition=edition)
        editions = reader.list_daily_editions()

    assert edition is not None
    assert [card.title for card in cards] == ["Replacement edition"]
    assert [row.edition_date for row in editions] == ["2026-08-18"]
