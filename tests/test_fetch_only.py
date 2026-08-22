from __future__ import annotations

import json
from datetime import datetime, timezone

from app.domain.models import FetchBatch, FetchItem, SourceSpec
from app.jobs.fetch_job import run_intel_fetch_job
from app.jobs.fetch_only_job import run_fetch_only_job


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
