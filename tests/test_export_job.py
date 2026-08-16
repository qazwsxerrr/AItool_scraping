from __future__ import annotations

from datetime import datetime, timezone

from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventRankingSnapshot, IntelItem, Source


def test_export_reads_selected_snapshot_only(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        event = IntelEvent(event_key="title:export", title="Export event", summary_cn="summary", topic="model", display_score=80, first_seen_at=datetime.now(timezone.utc))
        session.add(event)
        session.flush()
        session.add(IntelEventRankingSnapshot(snapshot_key="latest", event_id=event.id, rank=1, display_score=80, selected=True))
        session.commit()
    result = run_intel_export_job(session_factory=session_factory, output_dir=tmp_path)
    assert result.exported == 1
    assert '"record_type": "intel_event"' in (tmp_path / "intel_items.jsonl").read_text()


def test_export_omits_excluded_items_and_removes_legacy_pending_artifact(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        source = Source(
            id="export-source",
            name="Export source",
            transport="feed",
            url="https://example.test/feed",
            content_class="official_model_company",
        )
        session.add(source)
        session.add(
            IntelItem(
                source_id=source.id,
                title="Excluded Stage B item",
                content_class="official_model_company",
                content_hash="excluded-stage-b-item".ljust(64, "0"),
                selection_score=99,
                status="analysis_filtered",
            )
        )
        event = IntelEvent(
            event_key="title:retained",
            title="Retained event",
            summary_cn="summary",
            topic="model",
            display_score=80,
            first_seen_at=datetime.now(timezone.utc),
        )
        session.add(event)
        session.flush()
        session.add(
            IntelEventRankingSnapshot(
                snapshot_key="latest",
                event_id=event.id,
                rank=1,
                display_score=80,
                selected=True,
            )
        )
        session.commit()

    legacy_pending = tmp_path / "intel_pending.jsonl"
    legacy_pending.write_text('{"title":"legacy audit"}\n', encoding="utf-8")

    result = run_intel_export_job(session_factory=session_factory, output_dir=tmp_path)

    digest = (tmp_path / "intel_digest.md").read_text(encoding="utf-8")
    assert result.exported == 1
    assert not legacy_pending.exists()
    assert "待处理" not in digest
    assert "Excluded Stage B item" not in digest
