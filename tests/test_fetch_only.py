from __future__ import annotations

import json
from datetime import datetime, timezone

from app.domain.models import FetchBatch, FetchItem, SourceSpec
from app.jobs.fetch_job import run_intel_fetch_job
from app.jobs.fetch_only_job import run_fetch_only_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import FetchAttempt, Source
from app.storage.repository import IntelRepository


def _source(
    source_id: str,
    *,
    group: str = "official_blog",
    default_limit: int = 30,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id.replace("_", " ").title(),
        transport="feed",
        url=f"https://example.test/{source_id}.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group=group,
        fetch_interval=1,
        default_limit=default_limit,
    )


class _Router:
    def __init__(self, batches):
        self.batches = batches
        self.calls = []
        self.limits = []
        self.request_headers = []

    def collect(self, source, limit, request_headers=None):
        self.calls.append(source.id)
        self.limits.append(limit)
        self.request_headers.append(dict(request_headers or {}))
        return self.batches[source.id]


def test_fetch_uses_registry_limits_unless_manually_overridden():
    research = _source("research", default_limit=6)
    executive = _source("executive", default_limit=20)
    batches = {
        source.id: FetchBatch(source=source, items=[], http_status=200, transport="httpx")
        for source in (research, executive)
    }

    registry_router = _Router(batches)
    run_intel_fetch_job(
        session_factory=None,
        sources=[research, executive],
        router=registry_router,
        limit_per_source=None,
        force=True,
        dry_run=True,
    )
    override_router = _Router(batches)
    run_intel_fetch_job(
        session_factory=None,
        sources=[research, executive],
        router=override_router,
        limit_per_source=30,
        force=True,
        dry_run=True,
    )

    assert registry_router.limits == [6, 20]
    assert override_router.limits == [30, 30]


def test_fetch_only_exports_diagnostic_rows_without_database_writes(tmp_path):
    official = _source("openai_news")
    x_official = _source("x_account_openai", group="x_official")
    x_social = _source("x_account_sam_altman", group="x_social")

    def batch(source, title):
        return FetchBatch(
            source=source,
            items=[
                FetchItem(
                    source_id=source.id,
                    external_id=f"{source.id}:1",
                    title=title,
                    url=f"{source.url}/item",
                    captured_at=datetime.now(timezone.utc),
                    raw_payload={"fixture": True},
                )
            ],
            http_status=200,
            transport="httpx",
        )

    result = run_fetch_only_job(
        sources=[official, x_official, x_social],
        router=_Router(
            {
                official.id: batch(official, "Official item"),
                x_official.id: batch(x_official, "Official X item"),
                x_social.id: batch(x_social, "Social X item"),
            }
        ),
        output_dir=tmp_path / "out",
        force=True,
    )

    assert result.fetch.total_inserted == 3
    assert result.export.exported == 3
    records = [json.loads(line) for line in (tmp_path / "out" / "fetch_items.jsonl").read_text().splitlines()]
    assert {record["source_id"] for record in records} == {official.id, x_official.id, x_social.id}
    assert all("item_id" not in record for record in records)
    assert all("run_id" not in json.dumps(record) for record in records)


def test_fetch_only_isolates_one_failed_source_from_successful_sources(tmp_path):
    good = _source("good_source")
    bad = _source("bad_source")
    good_batch = FetchBatch(
        source=good,
        items=[FetchItem(source_id=good.id, external_id="good:1", title="good item")],
        http_status=200,
        transport="httpx",
    )
    bad_batch = FetchBatch(
        source=bad,
        status="failed",
        error_code="upstream_503",
        error_message="HTTP 503",
        http_status=503,
        transport="httpx",
    )

    result = run_fetch_only_job(
        sources=[good, bad],
        router=_Router({good.id: good_batch, bad.id: bad_batch}),
        output_dir=tmp_path / "out",
        force=True,
    )

    assert result.fetch.stats[good.id].inserted == 1
    assert result.fetch.stats[bad.id].failed == 1
    assert result.fetch.total_failed == 1
    assert result.export.exported == 1


def test_fetch_only_reports_degraded_source_without_counting_failure(tmp_path):
    source = _source("x_account_empty", group="x_official").model_copy(
        update={"transport": "rsshub"}
    )
    degraded = FetchBatch(
        source=source,
        items=[],
        status="degraded",
        error_code="empty_feed",
        error_message="RSSHub X returned a valid feed with no entries",
        http_status=200,
        transport="httpx",
    )

    result = run_fetch_only_job(
        sources=[source],
        router=_Router({source.id: degraded}),
        output_dir=tmp_path / "out",
        force=True,
    )

    stats = result.fetch.stats[source.id]
    assert stats.status == "degraded"
    assert stats.error == "RSSHub X returned a valid feed with no entries"
    assert result.fetch.total_failed == 0
    assert result.fetch.total_degraded == 1
    assert result.export.exported == 0


def test_persisted_degraded_fetch_records_content_warning_without_backoff(tmp_path):
    source = _source("x_account_empty", group="x_official").model_copy(
        update={"transport": "rsshub"}
    )
    degraded = FetchBatch(
        source=source,
        items=[],
        status="degraded",
        error_code="empty_feed",
        error_message="RSSHub X returned a valid feed with no entries",
        http_status=200,
        response_bytes=846,
        transport="httpx",
    )
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'fetch.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        repo = IntelRepository(session)
        _, run = repo.start_daily_build(edition_date="2026-08-24", source_ids=[source.id])
        session.commit()
        run_id = run.id

    result = run_intel_fetch_job(
        session_factory=session_factory,
        sources=[source],
        router=_Router({source.id: degraded}),
        source_filter=source.id,
        force=True,
        run_id=run_id,
    )

    assert result.stats[source.id].status == "degraded"
    assert result.total_failed == 0
    with session_factory() as session:
        source_row = session.get(Source, source.id)
        attempt = session.query(FetchAttempt).filter_by(source_id=source.id).one()
        assert source_row is not None
        assert source_row.health_status == "degraded"
        assert source_row.last_error_code == "empty_feed"
        assert source_row.consecutive_failures == 0
        assert source_row.backoff_until is None
        assert attempt.status == "degraded"
        assert attempt.error_code == "empty_feed"
        assert attempt.items_fetched == 0
        assert attempt.response_bytes == 846


def test_diagnostic_fetch_uses_unconditional_requests(tmp_path):
    source = _source("conditional_source")
    batch = FetchBatch(
        source=source,
        items=[
            FetchItem(
                source_id=source.id,
                external_id="conditional:1",
                title="Current item",
                captured_at=datetime.now(timezone.utc),
            )
        ],
        http_status=200,
        etag='"etag-v1"',
        transport="httpx",
    )
    router = _Router({source.id: batch})

    first = run_intel_fetch_job(
        session_factory=None,
        sources=[source],
        router=router,
        force=True,
        dry_run=True,
    )
    second = run_intel_fetch_job(
        session_factory=None,
        sources=[source],
        router=router,
        force=True,
        dry_run=True,
    )

    assert first.total_fetched == second.total_fetched == 1
    assert router.request_headers == [{}, {}]
