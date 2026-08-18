from __future__ import annotations

from datetime import datetime, timezone
import json

from sqlalchemy import select

from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs.stage_d_job import StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIItemReview,
    IntelEvent,
    IntelEventStageDSnapshot,
    IntelItem,
    IntelRun,
    IntelRunStageTask,
    Source,
)
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _new_run(session_factory, reference: datetime) -> int:
    with session_factory() as session:
        run = IntelRepository(session).start_run(reference_time=reference)
        session.commit()
        return int(run.id)


def _add_reviewed_item(
    session_factory,
    *,
    run_id: int,
    reference: datetime,
    title: str,
    canonical_url: str,
    suffix: str,
) -> int:
    with session_factory() as session:
        source = Source(
            id=f"source-{run_id}-{suffix}",
            name=f"Source {suffix}",
            transport="feed",
            url=f"https://source-{run_id}-{suffix}.example/feed",
            content_class="official_model_company",
            source_group="official_blog",
        )
        session.add(source)
        session.flush()
        item = IntelItem(
            source_id=source.id,
            canonical_url=canonical_url,
            title=title,
            content_class="official_model_company",
            content_hash=(f"{run_id}-{suffix}-{title}" * 8)[:64],
            selection_score=80,
            status="analysis_failed",
            published_at=reference,
            captured_at=reference,
        )
        session.add(item)
        session.flush()
        repo = IntelRepository(session)
        repo.record_run_item(run_id, item.id)
        session.add(
            AIItemReview(
                item_id=item.id,
                run_id=run_id,
                content_class="official_model_company",
                topic="model",
                topics_json='["model"]',
                keywords_json='["release"]',
                entities_json=json.dumps([{"type": "company", "name": "OpenAI"}]),
                selection_score=80,
                status="success",
            )
        )
        session.commit()
        return int(item.id)


def _add_event(session, *, run_id: int, key: str, score: int) -> int:
    event = IntelEvent(
        event_key=key,
        title=key,
        summary_cn=key,
        topic="model",
        display_score=score,
        new_in_run_id=run_id,
        first_run_id=run_id,
        last_run_id=run_id,
    )
    session.add(event)
    session.flush()
    return int(event.id)


def _persist_cluster_projection(session, *, run_id: int, event_ids: list[int]) -> None:
    repo = IntelRepository(session)
    stage = repo.ensure_stage(run_id, "cluster")
    task = repo.ensure_stage_task(
        stage,
        subject_type="run",
        subject_id=run_id,
        target_run_id=run_id,
    )
    task.status = "succeeded"
    task.result = {
        "event_ids": event_ids,
        "current_event_ids": event_ids,
        "processed": len(event_ids),
    }
    repo.refresh_stage_status(stage)


def test_same_run_cluster_keeps_new_ids_compatible_and_persists_current_ids():
    session_factory = _db()
    reference = datetime(2026, 8, 16, 6, tzinfo=timezone.utc)
    run_id = _new_run(session_factory, reference)
    _add_reviewed_item(
        session_factory,
        run_id=run_id,
        reference=reference,
        title="Orchid model release",
        canonical_url="https://example.test/orchid",
        suffix="one",
    )

    first = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
    )
    assert first.events == 1
    assert first.event_ids == [1]
    assert first.current_event_ids == [1]

    _add_reviewed_item(
        session_factory,
        run_id=run_id,
        reference=reference,
        title="Orchid model release",
        canonical_url="https://example.test/orchid",
        suffix="two",
    )
    second = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
        force=True,
    )
    assert second.events == 0
    assert second.repeats == 1
    assert second.event_ids == []
    assert second.current_event_ids == [1]

    with session_factory() as session:
        task = session.scalar(
            select(IntelRunStageTask)
            .join(IntelRunStageTask.stage)
            .where(
                IntelRunStageTask.subject_type == "run",
                IntelRunStageTask.subject_id == str(run_id),
                IntelRunStageTask.stage.has(run_id=run_id, stage_name="cluster"),
            )
        )
        assert task is not None
        assert task.result["current_event_ids"] == [1]


def test_stage_d_uses_only_latest_cluster_projection_for_same_run():
    session_factory = _db()
    reference = datetime(2026, 8, 16, 6, tzinfo=timezone.utc)
    run_id = _new_run(session_factory, reference)
    snapshot_key = f"run-{run_id}-reprocess"
    with session_factory() as session:
        old_id = _add_event(session, run_id=run_id, key="old-event", score=95)
        current_id = _add_event(session, run_id=run_id, key="current-event", score=80)
        session.add(
            IntelEventStageDSnapshot(
                snapshot_key=snapshot_key,
                run_id=run_id,
                event_id=old_id,
                display_order=1,
                display_score=95,
                selected=True,
            )
        )
        _persist_cluster_projection(session, run_id=run_id, event_ids=[current_id])
        session.commit()

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        snapshot_key=snapshot_key,
        profile=StageDProfile(total_max=10),
    )
    assert result.processed == 1
    assert result.selected == 1

    with session_factory() as session:
        rows = list(
            session.scalars(
                select(IntelEventStageDSnapshot).where(
                    IntelEventStageDSnapshot.snapshot_key == snapshot_key
                )
            )
        )
        assert [row.event_id for row in rows] == [current_id]


def test_stage_d_missing_or_empty_cluster_projection_clears_snapshot():
    session_factory = _db()
    reference = datetime(2026, 8, 16, 6, tzinfo=timezone.utc)
    run_id = _new_run(session_factory, reference)
    snapshot_key = f"run-{run_id}-empty"
    with session_factory() as session:
        old_id = _add_event(session, run_id=run_id, key="old-event", score=95)
        session.add(
            IntelEventStageDSnapshot(
                snapshot_key=snapshot_key,
                run_id=run_id,
                event_id=old_id,
                display_order=1,
                display_score=95,
                selected=True,
            )
        )
        _persist_cluster_projection(session, run_id=run_id, event_ids=[])
        session.commit()

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        snapshot_key=snapshot_key,
        profile=StageDProfile(total_max=10),
    )
    assert result.processed == 0
    with session_factory() as session:
        assert session.scalar(
            select(IntelEventStageDSnapshot.id).where(
                IntelEventStageDSnapshot.snapshot_key == snapshot_key
            )
        ) is None

    missing_key = f"run-{run_id}-missing"
    result_missing = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        snapshot_key=missing_key,
        profile=StageDProfile(total_max=10),
    )
    assert result_missing.processed == 0
