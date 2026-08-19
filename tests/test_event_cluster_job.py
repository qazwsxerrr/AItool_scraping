from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.ai.event_resolution import EventResolution, event_resolution_client_from_settings, resolve_event_group
from app.config.settings import Settings
from app.jobs import event_cluster_job as event_cluster_module
from app.jobs.event_cluster_job import _semantic_match_components, canonical_event_key, cluster_candidates, github_repo_identity, normalize_event_title, run_event_cluster_from_settings, run_event_cluster_job
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
            item = IntelItem(source_id=row["source_id"], canonical_url=row.get("url"), external_id=row.get("external_id"), title=row["title"], summary=row.get("summary"), content_class="official_model_company", content_hash=f"{row['source_id']}:{index}".encode().hex().ljust(64, "0")[:64], selection_score=row.get("score", 70), status="candidate", captured_at=now)
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
                    summary_cn=row.get("summary"),
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


def test_github_repo_identity_prefers_external_id_and_normalizes_url_fallback():
    assert github_repo_identity(
        {
            "external_id": " github_repo:Owner/Repo.GIT ",
            "canonical_url": "https://github.com/other/project",
        }
    ) == "owner/repo"
    assert github_repo_identity("https://WWW.GITHUB.COM/Owner/Repo.GIT/issues/7") == "owner/repo"


def test_different_github_repositories_do_not_fuzzy_merge_identical_text():
    rows = [
        {
            "id": 1,
            "title": "AI platform release",
            "summary": "The project announces a new AI platform release for developers.",
            "url": "https://github.com/acme/platform-a",
            "external_id": "github_repo:acme/platform-a",
            "keywords": ["ai", "platform", "release"],
            "entities": [{"type": "project", "name": "AI platform"}],
        },
        {
            "id": 2,
            "title": "AI platform release",
            "summary": "The project announces a new AI platform release for developers.",
            "url": "https://github.com/acme/platform-b",
            "external_id": "github_repo:acme/platform-b",
            "keywords": ["ai", "platform", "release"],
            "entities": [{"type": "project", "name": "AI platform"}],
        },
    ]

    groups = cluster_candidates(rows)

    assert [len(group) for group in groups] == [1, 1]


def test_ambiguous_ai_cannot_merge_different_github_repositories():
    class Resolver:
        def __init__(self):
            self.calls = 0

        def resolve_event(self, _values):
            self.calls += 1
            return {"decision": "merge", "confidence": 99}

    resolver = Resolver()
    groups = event_cluster_module.resolve_ambiguous_group(
        [
            {
                "id": 1,
                "title": "AI platform release",
                "summary": "The project announces a new AI platform release for developers.",
                "url": "https://github.com/acme/platform-a",
                "external_id": "github_repo:acme/platform-a",
                "keywords": ["ai", "platform", "release"],
            },
            {
                "id": 2,
                "title": "AI platform release",
                "summary": "The project announces a new AI platform release for developers.",
                "url": "https://github.com/acme/platform-b",
                "external_id": "github_repo:acme/platform-b",
                "keywords": ["ai", "platform", "release"],
            },
        ],
        ai_client=resolver,
    )

    assert [len(group) for group in groups] == [1, 1]
    assert resolver.calls == 0


def test_same_github_repository_can_merge_across_sources():
    groups = cluster_candidates(
        [
            {
                "id": 1,
                "title": "AI platform release",
                "summary": "The project announces a new AI platform release for developers.",
                "url": "https://github.com/acme/platform",
                "external_id": "github_repo:acme/platform",
                "keywords": ["ai", "platform", "release"],
            },
            {
                "id": 2,
                "title": "AI platform release",
                "summary": "The project announces a new AI platform release for developers.",
                "url": "https://www.github.com/Acme/Platform.git?utm_source=feed",
                "external_id": "github_repo:ACME/PLATFORM",
                "keywords": ["ai", "platform", "release"],
            },
        ]
    )

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


def test_different_github_repository_does_not_reuse_semantic_history():
    session_factory = _db()
    shared = {
        "title": "AI platform release",
        "summary": "The project announces a new AI platform release for developers.",
        "keywords": ["ai", "platform", "release"],
        "entities": [{"type": "project", "name": "AI platform"}],
    }
    _seed(
        session_factory,
        [
            {
                **shared,
                "source_id": "repo-a",
                "url": "https://github.com/acme/platform-a",
                "external_id": "github_repo:acme/platform-a",
            }
        ],
    )
    first = run_event_cluster_job(session_factory=session_factory)
    assert first.events == 1

    _seed(
        session_factory,
        [
            {
                **shared,
                "source_id": "repo-b",
                "url": "https://github.com/acme/platform-b",
                "external_id": "github_repo:acme/platform-b",
            }
        ],
    )
    second = run_event_cluster_job(
        session_factory=session_factory,
        item_ids=[2],
        now=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    assert second.events == 1 and second.repeats == 0
    with session_factory() as session:
        assert session.query(IntelEvent).count() == 2
        assert {relation.event_id for relation in session.query(IntelEventItem).all()} == {1, 2}


def test_contaminated_multi_repo_history_is_not_semantically_reused():
    session_factory = _db()
    shared = {
        "title": "AI platform release",
        "summary": "The project announces a new AI platform release for developers.",
        "keywords": ["ai", "platform", "release"],
        "entities": [{"type": "project", "name": "AI platform"}],
    }
    _seed(
        session_factory,
        [
            {
                **shared,
                "source_id": "repo-a",
                "url": "https://github.com/acme/platform-a",
                "external_id": "github_repo:acme/platform-a",
            },
            {
                **shared,
                "source_id": "repo-b",
                "url": "https://github.com/acme/platform-b",
                "external_id": "github_repo:acme/platform-b",
            },
        ],
    )
    with session_factory() as session:
        event = IntelEvent(
            event_key="url:https://github.com/acme/platform-a",
            canonical_url="https://github.com/acme/platform-a",
            external_id="github_repo:acme/platform-a",
            title="AI platform release",
            summary_cn=shared["summary"],
            topic="model",
            display_score=80,
            first_seen_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        session.add(event)
        session.flush()
        repo = IntelRepository(session)
        for item_id in (1, 2):
            item = session.get(IntelItem, item_id)
            repo.upsert_event_item(
                event.id,
                item_id,
                source_id=item.source_id,
                source_group=item.source.source_group,
                identity_key=f"external:{item.external_id}",
            )
        session.commit()

    _seed(
        session_factory,
        [
            {
                **shared,
                "source_id": "repo-c",
                "url": "https://github.com/acme/platform-c",
                "external_id": "github_repo:acme/platform-c",
            }
        ],
    )
    result = run_event_cluster_job(
        session_factory=session_factory,
        item_ids=[3],
        now=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    assert result.events == 1 and result.repeats == 0
    with session_factory() as session:
        relation = session.query(IntelEventItem).filter_by(item_id=3).one()
        assert relation.event_id != 1


def test_same_title_with_different_urls_is_a_weak_candidate_not_exact_identity():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {
                "source_id": "a",
                "title": "Platform announcement",
                "url": "https://example.test/a",
                "summary": "The platform announced a developer program.",
            },
            {
                "source_id": "b",
                "title": "Platform announcement",
                "url": "https://example.test/b",
                "summary": "The platform announced a separate regional partnership.",
            },
        ],
    )

    result = run_event_cluster_job(session_factory=session_factory)

    assert result.events == 2
    with session_factory() as session:
        assert session.query(IntelEvent).count() == 2


def test_fuzzy_grouping_does_not_transitively_merge_a_b_and_b_c():
    groups = cluster_candidates(
        [
            {
                "id": 1,
                "title": "one two three four alpha",
                "summary_cn": "one two three four alpha",
                "keywords": ["one", "two", "three", "four", "alpha"],
                "entities": [{"type": "term", "name": value} for value in ("one", "two", "three", "four", "alpha")],
            },
            {
                "id": 2,
                "title": "one two three four five",
                "summary_cn": "one two three four five",
                "keywords": ["one", "two", "three", "four", "five"],
                "entities": [{"type": "term", "name": value} for value in ("one", "two", "three", "four", "five")],
            },
            {
                "id": 3,
                "title": "two three four five charlie",
                "summary_cn": "two three four five charlie",
                "keywords": ["two", "three", "four", "five", "charlie"],
                "entities": [{"type": "term", "name": value} for value in ("two", "three", "four", "five", "charlie")],
            },
        ],
        title_threshold=0.55,
    )

    assert [len(group) for group in groups] == [2, 1]


def test_first_party_x_identity_removes_legacy_social_only_event_flag():
    session_factory = _db()
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        source = Source(
            id="legacy_official_x",
            name="Legacy official X",
            transport="rsshub",
            url="https://rsshub.example/twitter/user/legacy",
            source_group="x_official",
            source_subtype="account",
            source_role="official",
            # Simulate a row written before the official-X policy change.
            content_class="community_social",
        )
        item = IntelItem(
            source=source,
            canonical_url="https://x.com/legacy/status/1",
            title="Official launch",
            content_class="community_social",
            content_hash="l" * 64,
            selection_score=90,
            status="candidate",
            captured_at=now,
        )
        review = AIItemReview(
            item=item,
            content_class="community_social",
            topic="model",
            topics_json='["model"]',
            selection_score=90,
            risk_flags_json='["source:social_only"]',
            status="success",
        )
        session.add_all([source, item, review])
        session.commit()

    result = run_event_cluster_job(session_factory=session_factory)
    assert result.events == 1
    with session_factory() as session:
        event = session.query(IntelEvent).one()
        assert "source:social_only" not in json.loads(event.risk_flags_json)


def test_ambiguous_group_can_be_split_by_narrow_resolver():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {
                "source_id": "a",
                "title": "Orchid Systems processor announcement",
                "summary": "Orchid Systems announced a new processor launch for developers.",
                "keywords": ["orchid", "processor", "launch"],
                "entities": [{"type": "company", "name": "Orchid Systems"}],
            },
            {
                "source_id": "b",
                "title": "Orchid Systems processor launch",
                "summary": "Orchid Systems published a processor launch update for developers.",
                "keywords": ["orchid", "processor", "launch"],
                "entities": [{"type": "company", "name": "Orchid Systems"}],
            },
        ],
    )

    class Resolver:
        def __init__(self):
            self.values = []

        def resolve_event(self, values):
            self.values.append(values)
            return {"decision": "separate", "confidence": 95}

    resolver = Resolver()
    result = run_event_cluster_job(session_factory=session_factory, ai_client=resolver)
    assert result.ambiguous == 1 and result.ai_resolved == 1 and result.events == 2
    assert all(value.get("summary_cn") for value in resolver.values[0])


def test_ambiguous_group_accepts_ai_partition_only_when_it_covers_every_item():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {
                "source_id": "a",
                "title": "Orchid Systems processor announcement",
                "summary": "Orchid Systems announced a processor launch for developers.",
                "keywords": ["orchid", "processor", "launch"],
                "entities": [{"type": "company", "name": "Orchid Systems"}],
            },
            {
                "source_id": "b",
                "title": "Orchid Systems processor launch",
                "summary": "Orchid Systems published a processor launch update for developers.",
                "keywords": ["orchid", "processor", "launch"],
                "entities": [{"type": "company", "name": "Orchid Systems"}],
            },
        ],
    )

    class Resolver:
        def resolve_event(self, values):
            return {
                "decision": "partition",
                "confidence": 95,
                "groups": [[row["id"]] for row in values],
            }

    result = run_event_cluster_job(session_factory=session_factory, ai_client=Resolver())
    assert result.ambiguous == 1 and result.ai_resolved == 1 and result.events == 2


def test_strong_current_group_merges_without_resolver_call():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {
                "source_id": "a",
                "title": "OpenAI announces GPT-5 developer release",
                "summary": "OpenAI announced GPT-5 availability for developers today.",
                "keywords": ["gpt-5", "release"],
                "entities": [{"type": "company", "name": "OpenAI"}],
            },
            {
                "source_id": "b",
                "title": "OpenAI announces GPT-5 developer release",
                "summary": "OpenAI announced GPT-5 availability for developers today.",
                "keywords": ["GPT 5", "release"],
                "entities": [{"entity_type": "company", "text": "openai"}],
            },
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
        "summary_cn": "Orchid Systems announced an accelerator release for developers.",
        "keywords": ["gpt-5", "release"],
        "entities": [{"type": "company", "name": "OpenAI"}],
    }
    right = {
        "title": "Orchid Systems processor",
        "summary_cn": "Orchid Systems announced a processor release for developers.",
        "keywords_json": json.dumps(["GPT 5", "release"]),
        "entities_json": json.dumps([{"entity_type": "company", "text": "openai"}]),
    }
    title_score, keyword_score, entity_score, combined_score = _semantic_match_components(left, right)
    assert title_score == 0.5
    assert keyword_score == 1.0
    assert entity_score == 1.0
    assert combined_score >= 0.70

    weak = {**left, "title": "Unrelated platform launch", "summary_cn": "A different unrelated product announcement."}
    assert _semantic_match_components(weak, right)[3] < 0.55


def test_history_weak_metadata_overlap_is_not_an_exact_repeat():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {
                "source_id": "a",
                "title": "Orchid Systems processor",
                "summary": "Orchid Systems announced a processor product.",
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
                "summary": "Orchid Systems announced a separate accelerator product.",
                "keywords": ["GPT 5", "release"],
                "entities": [{"entity_type": "company", "text": "openai"}],
            }
        ],
    )
    second = run_event_cluster_job(
        session_factory=session_factory,
        item_ids=[2],
        now=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert second.events == 1 and second.repeats == 0 and second.event_ids == [2]


def test_subthreshold_history_match_uses_resolver_instead_of_auto_repeat():
    session_factory = _db()
    _seed(
        session_factory,
        [
            {
                "source_id": "a",
                "title": "Orchid Systems processor announcement",
                "summary": "Orchid Systems announced a processor launch for developers.",
                "keywords": ["orchid", "processor", "launch"],
                "entities": [{"type": "company", "name": "Orchid Systems"}],
            }
        ],
    )
    run_event_cluster_job(session_factory=session_factory)
    _seed(
        session_factory,
        [
            {
                "source_id": "b",
                "title": "Orchid Systems processor launch",
                "summary": "Orchid Systems published a processor launch update for developers.",
                "keywords": ["orchid", "processor", "launch"],
                "entities": [{"type": "company", "name": "Orchid Systems"}],
            }
        ],
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


def test_event_resolution_instance_passthrough_does_not_trigger_pairwise_fallback():
    calls = []

    def resolver(*args):
        calls.append(args)
        return EventResolution("separate", 95, "different repositories")

    evidence = resolve_event_group([{"id": 1}, {"id": 2}], resolver)

    assert evidence.decision == "separate"
    assert evidence.confidence == 95
    assert len(calls) == 1 and len(calls[0]) == 1
