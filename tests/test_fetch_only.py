from __future__ import annotations

import json
from datetime import datetime, timezone

from app.domain.models import FetchBatch, FetchItem, SourceSpec
from app.jobs.fetch_only_job import run_fetch_only_export_job, run_fetch_only_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem
from sqlalchemy import select


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'fetch-only.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _source(source_id: str, *, group: str = "official_blog") -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id.replace("_", " ").title(),
        transport="feed",
        url=f"https://example.test/{source_id}.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group=group,
        source_subtype="fixed_news",
        tier="p1" if group == "official_blog" else "p4",
        source_role="official" if group == "official_blog" else "social",
        content_class="official_model_company" if group == "official_blog" else "community_social",
        fetch_interval=1,
    )


class _Router:
    def __init__(self, batches):
        self.batches = batches
        self.calls = []

    def collect(self, source, limit, request_headers=None):
        self.calls.append(source.id)
        return self.batches[source.id]


def test_fetch_only_exports_new_rows_with_governance_and_x_marker(tmp_path):
    sf = _db(tmp_path)
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

    router = _Router(
        {
            official.id: batch(official, "Official item"),
            x_official.id: batch(x_official, "Official X item"),
            x_social.id: batch(x_social, "Social X item"),
        }
    )
    result = run_fetch_only_job(
        session_factory=sf,
        sources=[official, x_official, x_social],
        router=router,
        output_dir=tmp_path / "out",
        force=True,
    )
    assert result.fetch.total_inserted == 3
    assert result.export is not None
    records = [json.loads(line) for line in (tmp_path / "out" / "fetch_items.jsonl").read_text().splitlines()]
    assert {record["source_id"] for record in records} == {official.id, x_official.id, x_social.id}
    official_record = next(record for record in records if record["source_id"] == x_official.id)
    social_record = next(record for record in records if record["source_id"] == x_social.id)
    for key in ("source_id", "source_name", "transport", "source_group", "source_subtype", "tier", "role"):
        assert key in official_record
        assert key in official_record["source"]
    assert official_record["x_official"] is True
    assert social_record["x_official"] is False
    assert official_record["status"] == "new"
    with sf() as session:
        assert len(session.scalars(select(IntelItem)).all()) == 3


def test_fetch_only_export_dry_run_does_not_write_files(tmp_path):
    sf = _db(tmp_path)
    result = run_fetch_only_export_job(session_factory=sf, output_dir=tmp_path / "none", dry_run=True)
    assert result.exported == 0
    assert not (tmp_path / "none").exists()


def test_fetch_only_isolates_one_failed_source_from_successful_sources(tmp_path):
    sf = _db(tmp_path)
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
        session_factory=sf,
        sources=[good, bad],
        router=_Router({good.id: good_batch, bad.id: bad_batch}),
        output_dir=tmp_path / "out",
        force=True,
    )
    assert result.fetch.stats[good.id].inserted == 1
    assert result.fetch.stats[bad.id].failed == 1
    assert result.fetch.total_failed == 1
    assert result.export is not None
    assert result.export.exported == 1
