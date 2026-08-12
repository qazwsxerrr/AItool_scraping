from __future__ import annotations

import json
from datetime import datetime, timezone

from app.domain.models import FetchItem, SourceSpec
from app.jobs.daily_export_job import run_daily_export_job
from app.jobs.compose_job import run_compose_job
from app.jobs.cluster_job import _candidate_values
from app.jobs.enrich_job import run_enrich_job
from app.pipeline.event_cluster import canonical_event_key
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import DailyEdition, DailyEventEntry, Document, Event, EventEditorialReview, EventEvidence, IntelItem, Source
from app.ai.client import StageCallResult
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
        events_snapshot = json.loads(edition.events_json)
        assert isinstance(events_snapshot, list)
        assert "InstanceState" not in edition.events_json
        assert "Source object at" not in edition.events_json


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
    with session_factory() as session:
        edition = session.scalar(select(DailyEdition))
        snapshot = json.loads(edition.events_json)
        assert len(snapshot) == 4
        assert "InstanceState" not in edition.events_json
        assert "Source object at" not in edition.events_json
        entries = list(session.scalars(select(DailyEventEntry).where(DailyEventEntry.edition_id == edition.id)).all())
        assert len(entries) == 4
        rendered = json.loads(entries[0].rendered_json)
        assert isinstance(rendered.get("source"), dict)
        assert isinstance(rendered.get("primary_document"), dict)
        assert "InstanceState" not in entries[0].rendered_json
        assert "Source object at" not in entries[0].rendered_json


def test_cluster_candidate_preserves_exact_identity_fields():
    now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    github_item = IntelItem(
        source_id="github_release",
        external_id="github_repo:OpenAI/Example",
        title="Example v1.2.3",
        canonical_url="https://github.com/OpenAI/Example/releases/tag/v1.2.3",
        source_url="https://github.com/OpenAI/Example/releases/tag/v1.2.3",
        metrics_json=json.dumps({"full_name": "OpenAI/Example", "release_tag": "v1.2.3"}),
        raw_payload_json="{}",
        content_hash="a" * 64,
        content_class="project_tool",
        captured_at=now,
    )
    candidate = _candidate_values(github_item)
    assert candidate["external_id"] == "github_repo:OpenAI/Example"
    assert candidate["repository"] == "OpenAI/Example"
    assert canonical_event_key(candidate) == "github:openai/example@v1.2.3"

    arxiv_item = IntelItem(
        source_id="research",
        external_id="arxiv:2401.12345",
        title="A paper",
        metrics_json="{}",
        raw_payload_json="{}",
        content_hash="b" * 64,
        content_class="research_paper",
        captured_at=now,
    )
    assert canonical_event_key(_candidate_values(arxiv_item)) == "arxiv:2401.12345"

    doi_item = IntelItem(
        source_id="research",
        external_id="doi:10.1234/demo",
        title="A DOI paper",
        metrics_json="{}",
        raw_payload_json="{}",
        content_hash="c" * 64,
        content_class="research_paper",
        captured_at=now,
    )
    assert canonical_event_key(_candidate_values(doi_item)) == "doi:10.1234/demo"


def test_compose_persists_failed_editorial_raw_and_error(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'compose.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    with session_factory() as session:
        source = Source(
            id="compose_source",
            name="Compose source",
            transport="feed",
            url="https://compose.example/feed.xml",
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
        item = IntelItem(
            source_id=source.id,
            title="Compose candidate",
            canonical_url="https://compose.example/item",
            content_hash="f" * 64,
            content_class="official_model_company",
            status="selected",
            discovered_at=now,
            captured_at=now,
        )
        session.add(item)
        session.flush()
        event = Event(
            canonical_key="url:https://compose.example/item",
            canonical_url=item.canonical_url,
            section="model_product",
            event_type="release",
            event_hint=item.title,
            title=item.title,
            state="candidate",
            score=90,
            primary_item_id=item.id,
            primary_source_id=source.id,
            discovered_at=now,
        )
        session.add(event)
        session.flush()
        session.add(EventEvidence(evidence_key="compose-ev", event_id=event.id, item_id=item.id, role="primary", support_level="direct", is_primary=True, citation_url=item.canonical_url))
        session.commit()

    class FailingAI:
        def write_event(self, event, evidence):
            return StageCallResult(stage="write_event", status="request_error", raw={"provider": "offline"}, error="offline provider")

    result = run_compose_job(session_factory=session_factory, ai_client=FailingAI(), limit=10, now=now)
    assert result.failed == 1
    with session_factory() as session:
        review = session.scalar(select(EventEditorialReview))
        assert review is not None
        assert review.status == "request_error"
        assert review.error_message == "offline provider"
        assert json.loads(review.raw_response_json) == {"provider": "offline"}
