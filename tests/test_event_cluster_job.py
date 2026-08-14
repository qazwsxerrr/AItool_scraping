from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.jobs.event_cluster_job import (
    canonical_event_key,
    cluster_candidates,
    normalize_event_title,
    run_event_cluster_job,
)
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventItem, IntelEventRankingSnapshot, IntelItem, Source


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _seed_source_and_items(session_factory, rows):
    with session_factory() as session:
        source_ids = sorted({row["source_id"] for row in rows})
        for source_id in source_ids:
            session.add(
                Source(
                    id=source_id,
                    name=source_id,
                    transport="feed",
                    url=f"https://{source_id}.example.test/feed",
                    content_class="official_model_company",
                    source_group=source_id,
                )
            )
        session.flush()
        for index, row in enumerate(rows, start=1):
            title = row["title"]
            session.add(
                IntelItem(
                    source_id=row["source_id"],
                    canonical_url=row.get("url"),
                    external_id=row.get("external_id"),
                    title=title,
                    summary=row.get("summary"),
                    content_class="official_model_company",
                    content_hash=hashlib.sha256(f"{index}:{title}".encode()).hexdigest(),
                    selection_score=row.get("score", 40),
                    status="selected",
                    captured_at=datetime.now(timezone.utc),
                )
            )
        session.commit()


def test_exact_identity_dedupe_preserves_member_and_source_lineage_on_rerun():
    session_factory = _db()
    _seed_source_and_items(
        session_factory,
        [
            {"source_id": "source_a", "title": "Foo Release", "url": "https://example.test/foo/?utm_source=rss"},
            {"source_id": "source_b", "title": "foo   release", "url": "https://EXAMPLE.test/foo"},
        ],
    )

    first = run_event_cluster_job(session_factory=session_factory)
    second = run_event_cluster_job(session_factory=session_factory)

    with session_factory() as session:
        events = session.query(IntelEvent).all()
        members = session.query(IntelEventItem).all()
        snapshots = session.query(IntelEventRankingSnapshot).all()

    assert first.events == 1
    assert second.events == 0
    assert len(events) == 1
    assert len(members) == 2
    assert len(snapshots) == 1
    assert events[0].novelty_status == "unknown"
    assert events[0].topic == "unknown"
    assert set(__import__("json").loads(events[0].source_ids_json)) == {"source_a", "source_b"}


def test_exact_external_id_and_normalized_title_keys_are_stable():
    assert normalize_event_title("  Model—Release  v1.0  ") == "model release v1 0"
    assert canonical_event_key({"url": "https://example.test/a/?utm_medium=x"}) == "url:https://example.test/a"
    assert canonical_event_key({"external_id": " GUID 42 "}) == "external:guid42"
    assert canonical_event_key({"title": "同一 事件！"}) == "title:同一事件"

    groups = cluster_candidates(
        [
            {"title": "同一事件！", "topic": "product"},
            {"title": "同一 事件", "topic": "product"},
        ]
    )
    assert len(groups) == 1


def test_ambiguous_group_can_be_resolved_by_ai_without_losing_deterministic_fallback():
    session_factory = _db()
    _seed_source_and_items(
        session_factory,
        [
            {"source_id": "source_a", "title": "Open Model v1 release", "score": 80},
            {"source_id": "source_b", "title": "Open Model v1 发布", "score": 70},
        ],
    )

    class Resolver:
        def resolve_event(self, values):
            return {"decision": "separate", "confidence": 95}

    result = run_event_cluster_job(session_factory=session_factory, ai_client=Resolver())
    with session_factory() as session:
        events = session.query(IntelEvent).all()

    assert result.ai_resolved == 1
    assert len(events) == 2
    assert all(event.resolution_method == "ai_separate" for event in events)


def test_missing_72_hour_history_is_unknown_and_never_rejects_event():
    session_factory = _db()
    _seed_source_and_items(session_factory, [{"source_id": "source_a", "title": "A new item"}])

    result = run_event_cluster_job(
        session_factory=session_factory,
        history_hook=lambda values, **kwargs: None,
    )
    with session_factory() as session:
        event = session.query(IntelEvent).one()

    assert result.failed == 0
    assert event.novelty_status == "unknown"
    assert event.state == "candidate"
