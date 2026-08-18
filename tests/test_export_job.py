from __future__ import annotations

import json
from datetime import datetime, timezone

from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventStageDSnapshot, IntelItem, Source
from app.storage.repository import IntelRepository


def test_export_reads_selected_snapshot_only(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        event = IntelEvent(event_key="title:export", title="Export event", summary_cn="summary", topic="model", display_score=80, first_seen_at=datetime.now(timezone.utc))
        session.add(event)
        session.flush()
        session.add(IntelEventStageDSnapshot(snapshot_key="latest", event_id=event.id, display_order=1, display_score=80, selected=True, metadata_json='{"display_title_zh":"导出事件展示标题","title_supporting_fields":["title"]}'))
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
            IntelEventStageDSnapshot(
                snapshot_key="latest",
                event_id=event.id,
                display_order=1,
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


def test_run_scoped_export_writes_a_date_bundle_with_manifest(tmp_path):
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    session_factory = create_session_factory(engine)
    reference = datetime(2026, 8, 16, 6, 35, 31, tzinfo=timezone.utc)
    with session_factory() as session:
        run = IntelRepository(session).start_run(reference_time=reference)
        event = IntelEvent(
            event_key="title:daily-export",
            title="Daily export event",
            summary_cn="summary",
            topic="model",
            display_score=80,
            first_seen_at=reference,
        )
        session.add(event)
        session.flush()
        session.add(
            IntelEventStageDSnapshot(
                snapshot_key="daily-2026-08-16",
                run_id=run.id,
                event_id=event.id,
                display_order=1,
                display_score=80,
                selected=True,
                metadata_json=json.dumps(
                    {
                        "daily_repeat_prior_run_id": 7,
                        "daily_repeat_prior_snapshot_key": "run-7",
                        "material_update": True,
                    }
                ),
            )
        )
        session.commit()
        run_id = int(run.id)

    result = run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "intel",
        run_id=run_id,
    )

    daily_dir = tmp_path / "daily" / "2026-08-16"
    assert result.jsonl_path == str(daily_dir / "intel_items.jsonl")
    assert result.manifest_path == str(daily_dir / "manifest.json")
    manifest = json.loads((daily_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert manifest["edition_date"] == "2026-08-16"
    assert manifest["edition_timezone"] == "Asia/Shanghai"
    assert manifest["edition_status"] == "ready"
    assert "run_id" not in manifest
    assert "snapshot_key" not in manifest
    assert manifest["selected_count"] == 1
    assert manifest["funnel"]["stage_d_total"] == 1
    assert manifest["funnel"]["stage_d_selected"] == 1
    assert manifest["stages"]["export"]["status"] == "succeeded"
    assert manifest["stages"]["export"]["task_counts"]["succeeded"] == 1
    assert manifest["failure_reasons"] == []
    assert (daily_dir / "intel_digest.md").read_text(encoding="utf-8").startswith("# AI 日报 · 2026-08-16\n")
    record = json.loads((daily_dir / "intel_items.jsonl").read_text(encoding="utf-8"))
    assert record["metadata"] == {"material_update": True}
    assert "run_id" not in (daily_dir / "intel_items.jsonl").read_text(encoding="utf-8")
    assert "snapshot_key" not in (daily_dir / "intel_items.jsonl").read_text(encoding="utf-8")
    assert not (tmp_path / "runs").exists()
