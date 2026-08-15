from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select

from app.jobs.editorial_rank_job import EditorialProfile, run_editorial_rank_job
from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIItemReview,
    IntelEvent,
    IntelEventRankingSnapshot,
    IntelItem,
    IntelRun,
    IntelRunStage,
    IntelRunStageTask,
    Source,
)
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _run_with_item(
    session_factory,
    *,
    reference_time: datetime,
    title: str,
    score: int = 80,
    run_id: int | None = None,
):
    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, run_id) if run_id is not None else repo.start_run(reference_time=reference_time)
        assert run is not None
        source_id = f"source-{run.id}"
        source = session.get(Source, source_id)
        if source is None:
            source = Source(
                id=source_id,
                name="Source",
                transport="feed",
                url="https://source.example/feed",
                content_class="official_model_company",
            )
            session.add(source)
            session.flush()
        item = IntelItem(
            source_id=source.id,
            title=title,
            content_class="official_model_company",
            content_hash=(f"{run.id}-{title}" * 8)[:64],
            selection_score=score,
            status="analysis_failed",  # deliberately not the orchestration input
            captured_at=reference_time,
        )
        session.add(item)
        session.flush()
        repo.record_run_item(run.id, item.id)
        session.add(
            AIItemReview(
                item_id=item.id,
                run_id=run.id,
                content_class="official_model_company",
                topic="model",
                topics_json='["model"]',
                keywords_json='["gpt-5", "release"]',
                entities_json=json.dumps([{"type": "company", "name": "OpenAI"}]),
                selection_score=score,
                status="success",
            )
        )
        session.commit()
        return run.id, item.id


def test_cluster_uses_frozen_reference_time_and_run_projection_on_retry():
    session_factory = _db()
    reference = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    run_id, _ = _run_with_item(session_factory, reference_time=reference, title="Orchid Systems processor")

    first = run_event_cluster_job(session_factory=session_factory, run_id=run_id, reference_time=reference)
    assert first.events == 1
    assert first.reference_time == reference

    # A later retry sees a new Stage-B projection in the same run, while the
    # global item status intentionally still says analysis_failed.
    _, second_item_id = _run_with_item(
        session_factory,
        reference_time=reference,
        title="Orchid Systems accelerator",
        run_id=run_id,
    )
    assert second_item_id > 0

    second = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=reference + timedelta(hours=100),
    )
    assert second.reference_time == reference
    assert second.repeats == 1

    with session_factory() as session:
        stage = session.scalar(
            select(IntelRunStage).where(
                IntelRunStage.run_id == run_id,
                IntelRunStage.stage_name == "cluster",
            )
        )
        task = session.scalar(
            select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id)
        )
        assert task.status == "succeeded"


def test_rank_and_export_are_run_scoped_and_partial_export_preserves_digest(tmp_path):
    session_factory = _db()
    reference = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    with session_factory() as session:
        repo = IntelRepository(session)
        run_one = repo.start_run(reference_time=reference)
        run_two = repo.start_run(reference_time=reference)
        first = IntelEvent(
            event_key="title:one",
            title="Run one",
            summary_cn="one",
            topic="model",
            display_score=90,
            new_in_run_id=run_one.id,
            first_run_id=run_one.id,
        )
        second = IntelEvent(
            event_key="title:two",
            title="Run two",
            summary_cn="two",
            topic="model",
            display_score=80,
            new_in_run_id=run_two.id,
            first_run_id=run_two.id,
        )
        session.add_all([first, second])
        session.flush()
        session.add_all(
            [
                IntelEventRankingSnapshot(
                    snapshot_key=f"run-{run_one.id}",
                    run_id=run_one.id,
                    event_id=first.id,
                    rank=1,
                    display_score=90,
                    selected=True,
                ),
                IntelEventRankingSnapshot(
                    snapshot_key=f"run-{run_two.id}",
                    run_id=run_two.id,
                    event_id=second.id,
                    rank=1,
                    display_score=80,
                    selected=True,
                ),
            ]
        )
        session.commit()
        run_one_id, run_two_id = run_one.id, run_two.id

    ranked = run_editorial_rank_job(
        session_factory=session_factory,
        run_id=run_one_id,
        profile=EditorialProfile(total_max=1),
    )
    assert ranked.snapshot_key == f"run-{run_one_id}"
    assert ranked.selected == 1

    final_dir = tmp_path / "intel"
    final_dir.mkdir()
    (final_dir / "intel_digest.md").write_text("previous-success", encoding="utf-8")
    partial = run_intel_export_job(
        session_factory=session_factory,
        output_dir=final_dir,
        run_id=run_one_id,
        partial=True,
        partial_reason="upstream_failed",
    )
    assert (final_dir / "intel_digest.md").read_text(encoding="utf-8") == "previous-success"
    assert (tmp_path / "runs" / f"run-{run_one_id}" / "intel_digest.md").exists()

    successful = run_intel_export_job(
        session_factory=session_factory,
        output_dir=final_dir,
        run_id=run_one_id,
    )
    assert successful.exported == 1
    assert (final_dir / "intel_digest.md").read_text(encoding="utf-8") != "previous-success"
    assert run_two_id != run_one_id
