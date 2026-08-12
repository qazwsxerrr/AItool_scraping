from __future__ import annotations

import json
from datetime import datetime, timezone

from app.domain.models import FetchItem, SourceSpec
from app.jobs.daily_export_job import run_daily_export_job
from app.jobs.enrich_job import run_enrich_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import DailyEdition, Document, Event, EventEditorialReview, EventEvidence, IntelItem, Source
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


def test_daily_export_passes_primary_source_and_document_to_publication_gates(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'published.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)

    with session_factory() as session:
        source = Source(
            id="official_primary",
            name="Official primary",
            transport="feed",
            url="https://official.example/feed.xml",
            feed_format="rss",
            feed_adapter="generic",
            source_group="official_blog",
            tier="p1",
            primary_eligible=True,
            citation_policy="primary",
            content_class="official_model_company",
        )
        session.add(source)
        session.flush()
        for index in range(4):
            item = IntelItem(
                source_id=source.id,
                title=f"Primary event {index}",
                canonical_url=f"https://official.example/{index}",
                content_hash=f"{index + 100:064d}",
                content_class="official_model_company",
                status="selected",
                discovered_at=now,
                captured_at=now,
            )
            session.add(item)
            session.flush()
            document = Document(
                item_id=item.id,
                source_id=source.id,
                canonical_url=item.canonical_url,
                source_url=item.canonical_url,
                title=item.title,
                content_excerpt="direct evidence",
                content_text="direct evidence",
                status="fetched",
                http_status=200,
            )
            session.add(document)
            session.flush()
            event = Event(
                canonical_key=f"url:{item.canonical_url}",
                canonical_url=item.canonical_url,
                section="model_product",
                event_type="release",
                event_hint=item.title,
                title=item.title,
                state="composed",
                score=90,
                primary_item_id=item.id,
                primary_document_id=document.id,
                primary_source_id=source.id,
                discovered_at=now,
            )
            session.add(event)
            session.flush()
            session.add(
                EventEvidence(
                    evidence_key=f"ev-{event.id}",
                    event_id=event.id,
                    item_id=item.id,
                    document_id=document.id,
                    role="primary",
                    support_level="direct",
                    is_primary=True,
                    citation_url=item.canonical_url,
                )
            )
            session.add(
                EventEditorialReview(
                    event_id=event.id,
                    title=item.title,
                    summary_cn="summary",
                    why_it_matters="why",
                    facts_json='[{"text":"fact","evidence_ids":["ev-%s"]}]' % event.id,
                    valid_evidence_ids_json='["ev-%s"]' % event.id,
                    status="success",
                )
            )
        session.commit()

    result = run_daily_export_job(
        session_factory=session_factory,
        edition_date="2026-08-12",
        output_dir=tmp_path / "output",
    )
    assert result.status == "blocked"
    assert any(failure["code"] == "event_count" for failure in result.failures)
    assert not any(failure["code"] == "model_product_primary" for failure in result.failures)
