from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from app.ai.schemas import ItemAnalysisResponse
from app.config.source_registry import SourceConfig
from app.domain.models import FetchBatch, FetchItem
from app.domain.policies import source_spec_from_config
from app.jobs.export_job import run_intel_export_job
from app.jobs.fetch_job import run_intel_fetch_job
from app.jobs.process_job import run_intel_process_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelRepository
from app.storage.models import AIItemReview, FetchAttempt, IntelItem, IntelItemVerification, Source
from sqlalchemy import select


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _source(**overrides) -> SourceConfig:
    values = {
        "id": "github_test",
        "name": "GitHub test",
        "transport": "github",
        "url": "https://api.github.com/search/repositories",
        "github": {"mode": "search", "query": "ai", "pushed_days": 7},
        "source_group": "github",
        "source_subtype": "search_repositories",
        "source_role": "code_hosting",
        "fetch_interval": 1,
    }
    values.update(overrides)
    return SourceConfig(**values)


def _community_source(**overrides) -> SourceConfig:
    values = {
        "id": "community_test",
        "name": "Community test",
        "transport": "feed",
        "url": "https://community.example/feed.xml",
        "feed": {"format": "rss", "adapter": "generic"},
        "source_group": "community",
        "source_subtype": "fixed",
        "source_role": "community",
        "fetch_interval": 1,
    }
    values.update(overrides)
    return SourceConfig(**values)


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'intel.db'}")
    init_db(engine)
    return create_session_factory(engine)


class _Router:
    def __init__(self, batches):
        self.batches = batches

    def collect(self, source, limit):
        return self.batches[source.id]


class _AI:
    model = "test-model"

    def __init__(self, *, fail_ids=()):
        self.calls = []
        self.fail_ids = set(fail_ids)

    def analyze(self, request):
        self.calls.append(request.item_id)
        if request.item_id in self.fail_ids:
            raise RuntimeError("model timeout")
        return ItemAnalysisResponse(
            keep=True,
            content_class=request.source_content_class,
            summary_cn="测试摘要",
            reason="测试保留",
            risk_flags=[],
            needs_verification=request.source_content_class == "official_model_company",
            official_url=request.url if request.source_content_class == "official_model_company" else None,
            confidence=88,
            raw_response={"keep": True},
        )


def test_fetch_is_idempotent_and_persists_attempt_telemetry(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    item = FetchItem(
        source_id=source.id,
        external_id="github_repo:7",
        title="GitHub repo: owner/project",
        url="https://github.com/owner/project",
        published_at=NOW - timedelta(days=1),
        metrics={"stars": 1200, "forks": 10, "pushed_at": (NOW - timedelta(days=1)).isoformat()},
        raw_payload={"github_item_type": "repository", "id": 7},
    )
    batch = FetchBatch(source=spec, items=[item], http_status=200, response_bytes=321, transport="github_api")
    first = run_intel_fetch_job(session_factory=sf, sources=[spec], router=_Router({source.id: batch}), force=True)
    second = run_intel_fetch_job(session_factory=sf, sources=[spec], router=_Router({source.id: batch}), force=True)

    assert (first.total_inserted, first.total_skipped) == (1, 0)
    assert (second.total_inserted, second.total_skipped) == (0, 1)
    with sf() as session:
        assert session.scalar(select(IntelItem)) is not None
        source_row = session.scalar(select(Source))
        assert json.loads(source_row.selection_policy_json)["mode"] == "github_active_high_star"
        assert json.loads(source_row.verification_policy_json)["mode"] == "metadata_only"
        attempt = session.scalars(select(FetchAttempt).order_by(FetchAttempt.id.desc())).first()
        assert attempt is not None
        assert attempt.status == "success"
        assert attempt.items_fetched == 1
        assert attempt.response_bytes == 321


def test_process_applies_github_threshold_without_ai_scoring_or_filtering(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="github_repo:100",
                title="low star",
                url="https://github.com/a/low",
                published_at=NOW - timedelta(days=1),
                metrics={"stars": 100, "pushed_at": (NOW - timedelta(days=1)).isoformat()},
            )
        )
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="github_repo:101",
                title="high star",
                url="https://github.com/a/high",
                published_at=NOW - timedelta(days=1),
                metrics={"stars": 101, "pushed_at": (NOW - timedelta(days=1)).isoformat()},
                raw_payload={"github_item_type": "repository", "license": {"spdx_id": "MIT"}},
            )
        )
        session.commit()

    ai = _AI()
    result = run_intel_process_job(session_factory=sf, source_specs={source.id: spec}, ai_client=ai, limit=10)
    assert result.processed == 2
    assert result.filtered == 1
    assert result.selected == 1
    assert result.analyzed == 1
    assert len(ai.calls) == 1
    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert rows[0].status == "filtered"
        assert rows[1].status == "hotspot"
        assert rows[1].selection_score == 0
        assert rows[1].ai_review.status == "success"
        assert rows[1].ai_review.keep is False
        assert rows[1].ai_review.summary_cn == "测试摘要"
        assert rows[1].verification.mode == "metadata_only"


def test_official_item_requires_successful_direct_link(tmp_path):
    sf = _db(tmp_path)
    source = SourceConfig(
        id="official_test",
        name="Official",
        transport="feed",
        url="https://official.example/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_role="official",
        fetch_interval=1,
    )
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                content_class=spec.content_class,
                title="Announcing a new model release",
                url="https://official.example/posts/model",
                published_at=NOW - timedelta(days=1),
                summary="model release",
            )
        )
        session.commit()

    class _HTTP:
        def get(self, url, **kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                content=b"<html><title>Official model release</title><p>release</p></html>",
            )

    ai = _AI()
    result = run_intel_process_job(
        session_factory=sf,
        source_specs={source.id: spec},
        ai_client=ai,
        http_client=_HTTP(),
        limit=10,
    )
    assert result.verified == 1
    with sf() as session:
        item = session.scalar(select(IntelItem))
        assert item.status == "verified"
        assert item.verification.status == "verified"
        assert item.verification.supports_basic_fact is True


def test_ai_failure_isolated_per_item_and_persisted(tmp_path):
    sf = _db(tmp_path)
    source = _community_source()
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        for number in (1, 2):
            repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=f"community:{number}",
                    title=f"AI community post {number}",
                    url=f"https://community.example/posts/{number}",
                    published_at=NOW - timedelta(days=1),
                    metrics={"engagement": 2},
                )
            )
        session.commit()
    ai = _AI(fail_ids={1})
    result = run_intel_process_job(session_factory=sf, source_specs={source.id: spec}, ai_client=ai, limit=10)
    assert result.ai_failed == 1
    assert result.analyzed == 1
    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert {row.status for row in rows} == {"ai_failed", "discovery_only"}
        assert session.scalar(select(AIItemReview).where(AIItemReview.status == "ai_failed")) is not None


def test_github_summary_failure_remains_hotspot_and_is_retryable(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="github_repo:retry/project",
                title="Retry project",
                url="https://github.com/retry/project",
                metrics={"stars": 900, "pushed_at": (NOW - timedelta(days=1)).isoformat()},
                raw_payload={"github_item_type": "repository", "full_name": "retry/project"},
            )
        )
        session.commit()

    class _FailingAI(_AI):
        def analyze(self, request):
            self.calls.append(request.item_id)
            raise RuntimeError("summary unavailable")

    ai = _FailingAI()
    result = run_intel_process_job(session_factory=sf, source_specs={source.id: spec}, ai_client=ai, limit=10)
    assert result.ai_failed == 1

    with sf() as session:
        row = session.scalar(select(IntelItem))
        assert row.status == "hotspot"
        assert row.ai_review.status == "ai_failed"
        pending = IntelRepository(session).list_pending_items(limit=None)
        assert [item.id for item in pending] == [row.id]


def test_export_contains_audit_fields_and_dry_run_does_not_write(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="github_repo:9",
                title="export me",
                url="https://github.com/a/export",
                published_at=NOW - timedelta(days=1),
                metrics={"stars": 999, "pushed_at": (NOW - timedelta(days=1)).isoformat()},
            )
        )
        session.commit()
    run_intel_process_job(session_factory=sf, source_specs={source.id: spec}, ai_client=_AI(), limit=10)
    output = tmp_path / "out"
    dry = run_intel_export_job(session_factory=sf, output_dir=output, dry_run=True)
    assert dry.exported == 1
    assert not output.exists()
    actual = run_intel_export_job(session_factory=sf, output_dir=output)
    assert actual.exported == 1
    record = json.loads((output / "intel_items.jsonl").read_text().splitlines()[0])
    assert record["metrics"]["stars"] == 999
    assert record["ai"]["summary_cn"] == "测试摘要"
    assert record["verification"]["mode"] == "metadata_only"


def test_community_links_are_retained_as_follow_up_candidates(tmp_path):
    sf = _db(tmp_path)
    source = SourceConfig(
        id="community_test",
        name="Community",
        transport="feed",
        url="https://community.example/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="community",
        source_role="community",
        fetch_interval=1,
    )
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                content_class=spec.content_class,
                title="社区发现",
                url="https://community.example/post",
                published_at=NOW - timedelta(days=1),
                content="试用 https://github.com/owner/tool 和 https://huggingface.co/models/demo",
            )
        )
        session.commit()
    result = run_intel_process_job(session_factory=sf, source_specs={source.id: spec}, ai_client=_AI(), limit=10)
    assert result.selected == 1
    with sf() as session:
        item = session.scalar(select(IntelItem))
        links = json.loads(item.discovered_links_json)
        assert {link["content_class"] for link in links} == {"project_tool", "official_model_company"}
        assert item.status == "discovery_only"


def test_verifier_failure_keeps_successful_ai_review_and_marks_needs_review(tmp_path):
    sf = _db(tmp_path)
    source = SourceConfig(
        id="official_error",
        name="Official",
        transport="feed",
        url="https://official.example/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_role="official",
        fetch_interval=1,
    )
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                title="Announcing model release",
                url="https://official.example/post",
                published_at=NOW - timedelta(days=1),
                summary="release",
            )
        )
        session.commit()

    class _BrokenHTTP:
        def get(self, url, **kwargs):
            raise RuntimeError("upstream unavailable")

    result = run_intel_process_job(
        session_factory=sf,
        source_specs={source.id: spec},
        ai_client=_AI(),
        http_client=_BrokenHTTP(),
        limit=10,
    )
    assert result.analyzed == 1
    assert result.ai_failed == 0
    assert result.needs_review == 1
    with sf() as session:
        item = session.scalar(select(IntelItem))
        assert item.status == "needs_review"
        assert item.ai_review.status == "success"
        assert item.verification.status == "failed"
