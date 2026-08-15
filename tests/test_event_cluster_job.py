from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.ai.event_resolution import event_resolution_client_from_settings
from app.config.settings import Settings
from app.jobs import event_cluster_job as event_cluster_module
from app.jobs.event_cluster_job import _semantic_match_components, canonical_event_key, cluster_candidates, normalize_event_title, run_event_cluster_from_settings, run_event_cluster_job
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
            session.add(
                AIItemReview(
                    item_id=item.id,
                    run_id=run_id,
                    content_class="official_model_company",
                    topic="model",
                    topics_json=json.dumps(row.get("topics", ["model"]), ensure_ascii=False),
                    keywords_json=json.dumps(row.get("keywords", ["model"]), ensure_ascii=False),
                    entities_json=json.dumps(row.get("entities", []), ensure_ascii=False),
                    selection_score=item.selection_score,
                    status="success",
                )
            )
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
    _seed(session_factory, [{"source_id": "a", "title": "Orchid Systems processor"}, {"source_id": "b", "title": "Orchid Systems launch"}])

    class Resolver:
        def resolve_event(self, values):
            return {"decision": "separate", "confidence": 95}

    result = run_event_cluster_job(session_factory=session_factory, ai_client=Resolver())
    assert result.ambiguous == 1 and result.ai_resolved == 1 and result.events == 2


def test_strong_current_group_merges_without_resolver_call():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {"source_id": "a", "title": "Orchid Systems processor", "keywords": ["gpt-5"], "entities": [{"type": "company", "name": "OpenAI"}]},
            {"source_id": "b", "title": "Orchid Systems accelerator", "keywords": ["GPT 5"], "entities": [{"entity_type": "company", "text": "openai"}]},
        ],
    )

    class Resolver:
        def __init__(self):
            self.calls = 0

        def resolve_event(self, values):
            self.calls += 1
            return {"decision": "separate", "confidence": 99}

    resolver = Resolver()
    result = run_event_cluster_job(session_factory=session_factory, ai_client=resolver)
    assert result.events == 1 and result.ambiguous == 0 and result.ai_resolved == 0
    assert resolver.calls == 0


def test_semantic_repeat_score_uses_keywords_and_typed_entities_with_threshold():
    left = {
        "title": "Orchid Systems accelerator",
        "keywords": ["gpt-5", "release"],
        "entities": [{"type": "company", "name": "OpenAI"}],
    }
    right = {
        "title": "Orchid Systems processor",
        "keywords_json": json.dumps(["GPT 5", "release"]),
        "entities_json": json.dumps([{"entity_type": "company", "text": "openai"}]),
    }
    title_score, keyword_score, entity_score, combined_score = _semantic_match_components(left, right)
    assert title_score == 0.5
    assert keyword_score == 1.0
    assert entity_score == 1.0
    assert combined_score >= 0.70

    weak = {**left, "title": "Unrelated platform launch"}
    assert _semantic_match_components(weak, right)[3] < 0.70


def test_history_repeat_uses_metadata_overlap_when_title_is_only_partial():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {
                "source_id": "a",
                "title": "Orchid Systems processor",
                "keywords": ["gpt-5", "release"],
                "entities": [{"type": "company", "name": "OpenAI"}],
            }
        ],
    )
    first = run_event_cluster_job(session_factory=session_factory)
    assert first.events == 1
    _seed(
        session_factory,
        [
            {
                "source_id": "b",
                "title": "Orchid Systems accelerator",
                "keywords": ["GPT 5", "release"],
                "entities": [{"entity_type": "company", "text": "openai"}],
            }
        ],
    )
    second = run_event_cluster_job(session_factory=session_factory, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert second.events == 0 and second.repeats == 1 and second.event_ids == []


def test_subthreshold_history_match_uses_resolver_instead_of_auto_repeat():
    session_factory = _db()
    _seed(
        session_factory,
        [{"source_id": "a", "title": "Orchid Systems processor", "keywords": ["release"]}],
    )
    run_event_cluster_job(session_factory=session_factory)
    _seed(
        session_factory,
        [{"source_id": "b", "title": "Orchid Systems launch", "keywords": ["release"]}],
    )

    class Resolver:
        def __init__(self):
            self.calls = []

        def resolve_event(self, values):
            self.calls.append(values)
            return {"decision": "separate", "confidence": 92, "evidence": "different announcements"}

    resolver = Resolver()
    second = run_event_cluster_job(session_factory=session_factory, item_ids=[2], ai_client=resolver, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert second.events == 1 and second.event_ids == [2]
    assert len(resolver.calls) == 1


def test_event_resolution_factory_and_injected_run_path(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"decision": "merge", "confidence": 88, "evidence": "same release"}

    class HTTP:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    http = HTTP()
    settings = Settings(
        ai_review_api_url="https://resolver.example/v1",
        ai_review_api_key="secret",
        ai_review_model="resolver-model",
        ai_review_api_style="generic_json",
    )
    client = event_resolution_client_from_settings(settings, http_client=http)
    assert client is not None
    evidence = client.resolve_event([{"title": "A"}, {"title": "B"}])
    assert evidence.merge and evidence.confidence == 88
    assert http.calls and http.calls[0][0] == "https://resolver.example/v1"

    captured = {}
    resolver = object()

    def fake_run(**kwargs):
        captured["kwargs"] = kwargs
        return event_cluster_module.EventClusterResult()

    monkeypatch.setattr(event_cluster_module, "event_resolution_client_from_settings", lambda value: resolver)
    monkeypatch.setattr(event_cluster_module, "run_event_cluster_job", fake_run)
    run_event_cluster_from_settings(settings=Settings(database_url="sqlite:///:memory:"))
    assert captured["kwargs"]["ai_client"] is resolver

    injected = object()
    monkeypatch.setattr(event_cluster_module, "event_resolution_client_from_settings", lambda value: (_ for _ in ()).throw(AssertionError("factory should not run")))
    run_event_cluster_from_settings(settings=Settings(database_url="sqlite:///:memory:"), ai_client=injected)
    assert captured["kwargs"]["ai_client"] is injected
