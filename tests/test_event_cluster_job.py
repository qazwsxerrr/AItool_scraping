from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.jobs.event_cluster_job import canonical_event_key, cluster_candidates, normalize_event_title, run_event_cluster_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEvent, IntelEventItem, IntelItem, Source
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _seed(session_factory, rows, run_id: int | None = None):
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        source_ids = sorted({row["source_id"] for row in rows})
        for source_id in source_ids:
            session.add(Source(id=source_id, name=source_id, transport="feed", url=f"https://{source_id}.example", content_class="official_model_company", source_group=source_id))
        session.flush()
        for index, row in enumerate(rows, start=1):
            item = IntelItem(source_id=row["source_id"], canonical_url=row.get("url"), external_id=row.get("external_id"), title=row["title"], content_class="official_model_company", content_hash=f"{row['source_id']}:{index}".encode().hex().ljust(64, "0")[:64], selection_score=row.get("score", 70), status="candidate", captured_at=now)
            session.add(item)
            session.flush()
            session.add(AIItemReview(item_id=item.id, run_id=run_id, content_class="official_model_company", topic="model", topics_json='["model"]', keywords_json='["model"]', selection_score=item.selection_score, status="success"))
        session.commit()


def test_identity_helpers_and_candidate_clusters_are_stable():
    assert normalize_event_title("  Model—Release  v1.0  ") == "model release v1 0"
    assert canonical_event_key({"url": "https://example.test/a/?utm_medium=x"}) == "url:https://example.test/a"
    assert canonical_event_key({"external_id": " GUID 42 "}) == "external:guid42"
    groups = cluster_candidates([{"title": "Open Model v1", "url": "https://example/a"}, {"title": "Open Model v1", "url": "https://example/a?utm_source=rss"}])
    assert len(groups) == 1 and len(groups[0]) == 2


def test_exact_repeat_attaches_lineage_without_new_event():
    session_factory = _db()
    _seed(session_factory, [{"source_id": "a", "title": "Foo Release", "url": "https://example.test/foo"}])
    first = run_event_cluster_job(session_factory=session_factory)
    assert first.events == 1 and first.event_ids == [1]
    _seed(session_factory, [{"source_id": "b", "title": "Foo Release", "url": "https://example.test/foo"}])
    second = run_event_cluster_job(session_factory=session_factory, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert second.events == 0 and second.event_ids == [] and second.repeats == 1
    with session_factory() as session:
        assert session.query(IntelEvent).count() == 1
        assert session.query(IntelEventItem).count() == 2


def test_ambiguous_group_can_be_split_by_narrow_resolver():
    session_factory = _db()
    _seed(session_factory, [{"source_id": "a", "title": "Open Model v1 release"}, {"source_id": "b", "title": "Open Model v1 发布"}])

    class Resolver:
        def resolve_event(self, values):
            return {"decision": "separate", "confidence": 95}

    result = run_event_cluster_job(session_factory=session_factory, ai_client=Resolver())
    assert result.ambiguous == 1 and result.ai_resolved == 1 and result.events == 2
