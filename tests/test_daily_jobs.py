from __future__ import annotations

import json

from app.domain.models import FetchItem, SourceSpec
from app.jobs.daily_export_job import run_daily_export_job
from app.jobs.enrich_job import run_enrich_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import Document, DailyEdition
from app.storage.repository import IntelRepository
from sqlalchemy import select


def _source() -> SourceSpec:
    return SourceSpec(
        id="daily_test",
        name="Daily test",
        transport="feed",
        url="https://example.test/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        content_class="official_model_company",
    )


def test_enrich_and_blocked_daily_export_use_no_network_sqlite(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'daily.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    source = _source()

    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="daily-item-1",
                title="A model update",
                url="https://example.test/post",
                summary="A bounded summary",
            )
        )
        session.commit()

    enriched = run_enrich_job(session_factory=session_factory, limit=10)
    assert enriched.processed == 1
    assert enriched.enriched == 1
    with session_factory() as session:
        assert session.scalar(select(Document)) is not None

    output_dir = tmp_path / "output"
    exported = run_daily_export_job(
        session_factory=session_factory,
        edition_date="2026-08-12",
        output_dir=output_dir,
    )
    assert exported.status == "blocked"
    assert exported.published is False
    assert exported.draft_path is not None
    assert exported.pending_path is not None
    assert (output_dir / "2026/08/2026-08-12.draft.md").exists()
    assert (output_dir / "2026/08/2026-08-12.pending.jsonl").exists()
    assert "BLOCKED" in (output_dir / "2026/08/2026-08-12.draft.md").read_text(encoding="utf-8")
    with session_factory() as session:
        edition = session.scalar(select(DailyEdition))
        assert edition is not None
        assert edition.status == "blocked"
        assert json.loads(edition.gate_results_json)["publishable"] is False
