from __future__ import annotations

from datetime import datetime, timezone

from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventRankingSnapshot


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
