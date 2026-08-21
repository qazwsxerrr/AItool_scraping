from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.ai.skills.stage_c_aggregation import (
    STAGE_C_SCHEMA_VERSION,
    StageCAggregationCallResult,
    StageCAggregationResponse,
)
from app.ai.skills.stage_d_selection import STAGE_D_SELECTION_SCHEMA_VERSION
from app.domain.models import FetchItem
from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs.export_job import run_intel_export_job
from app.jobs.stage_d_job import StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEventItem, IntelItem, IntelRunStage, IntelRunStageTask, Source
from app.storage.repository import IntelRepository


class _SelectionClient:
    model = "test-stage-d-selection"
    max_retries = 0

    def select(self, events, *, edition, max_selected):
        return {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {
                    "event_id": int(event["event_id"]),
                    "reason_code": "material_change",
                    "reason": "测试事件适合进入日报。",
                }
                for event in events[:max_selected]
            ],
        }


class _StageCClient:
    model = "test-stage-c"

    def aggregate(self, current_items, *, recent_history, edition):
        raw = {
            "schema_version": STAGE_C_SCHEMA_VERSION,
            "clusters": [
                {
                    "title_zh": str(item["title"]),
                    "summary_zh": str(item.get("summary_cn") or item["title"]),
                    "item_ids": [int(item["id"])],
                    "novelty_status": "new",
                    "prior_event_key": None,
                }
                for item in current_items
            ],
        }
        return StageCAggregationCallResult(
            parsed=StageCAggregationResponse.model_validate(raw),
            raw_response=raw,
            request_metadata={"model": self.model},
        )


class _FailingStageCClient:
    model = "test-stage-c-failure"

    def aggregate(self, current_items, *, recent_history, edition):
        raise RuntimeError("stage c provider failed")


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _build_with_stage_b_items(
    session_factory,
    *,
    reference_time: datetime,
    rows: list[tuple[str, int, str]],
) -> tuple[int, dict[str, int]]:
    """Seed one immutable daily build with completed Stage-B tasks."""

    with session_factory() as session:
        repo = IntelRepository(session)
        session.add(
            Source(
                id="downstream-source",
                name="Source",
                transport="feed",
                url="https://source.example/feed.xml",
                source_group="official_blog",
                content_class="official_model_company",
            )
        )
        session.flush()
        _, build = repo.start_daily_build(
            edition_date="2026-08-15",
            reference_time=reference_time,
        )
        analyze = repo.ensure_stage(build.id, "analyze")
        item_ids: dict[str, int] = {}
        for index, (title, score, status) in enumerate(rows, start=1):
            inserted = repo.insert_item(
                FetchItem(
                    source_id="downstream-source",
                    external_id=f"downstream-{index}",
                    title=title,
                    url=f"https://source.example/{index}",
                    summary=f"{title} summary",
                    content_class="official_model_company",
                    published_at=reference_time,
                    captured_at=reference_time,
                ),
                run_id=build.id,
            )
            assert inserted.item_id is not None
            item_id = int(inserted.item_id)
            item_ids[title] = item_id
            session.add(
                AIItemReview(
                    item_id=item_id,
                    content_class="official_model_company",
                    topic="model_release",
                    topics_json='["model_release"]',
                    keywords_json='["gpt-5", "release"]',
                    entities_json=json.dumps([{"type": "company", "name": "OpenAI"}]),
                    summary_cn=f"{title} summary",
                    b1_priority=score,
                    status="success",
                )
            )
            item = session.get(IntelItem, item_id)
            assert item is not None
            if status.startswith("analysis_filtered:"):
                item.status = "analysis_filtered"
            task = repo.ensure_stage_task(
                analyze,
                subject_type="item",
                subject_id=item_id,
                item_id=item_id,
            )
            repo.complete_stage_task(
                task,
                result={"item_id": item_id, "status": "success", "b1_priority": score},
            )
        repo.finish_stage(analyze, status="succeeded")
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id), item_ids


def test_cluster_retry_keeps_the_frozen_reference_time_and_current_build_projection():
    session_factory = _db()
    reference = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    run_id, _ = _build_with_stage_b_items(
        session_factory,
        reference_time=reference,
        rows=[("Orchid Systems processor", 80, "candidate")],
    )

    client = _StageCClient()
    first = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
        ai_client=client,
    )
    second = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        force=True,
        now=reference + timedelta(hours=100),
        ai_client=client,
    )

    assert first.events == 1
    assert first.candidate_event_ids == first.current_event_ids
    assert first.reference_time == reference
    assert second.reference_time == reference
    assert second.events == 1
    assert second.current_event_ids == first.current_event_ids
    with session_factory() as session:
        stage = session.scalar(
            select(IntelRunStage).where(
                IntelRunStage.run_id == run_id,
                IntelRunStage.stage_name == "cluster",
            )
        )
        task = session.scalar(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id))
        assert task is not None and task.status == "succeeded"


def test_cluster_excludes_structurally_filtered_stage_b_items():

    session_factory = _db()
    reference = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    run_id, item_ids = _build_with_stage_b_items(
        session_factory,
        reference_time=reference,
        rows=[
            ("Orchid Systems candidate", 85, "candidate"),
            ("Orchid Systems low signal", 42, "analysis_filtered:score_below_threshold"),
        ],
    )

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
        ai_client=_StageCClient(),
    )

    assert result.processed == 1
    assert result.events == 1
    with session_factory() as session:
        relations = session.scalars(select(IntelEventItem)).all()
        assert {relation.item_id for relation in relations} == {item_ids["Orchid Systems candidate"]}


def test_stage_d_and_export_use_only_the_current_daily_build(tmp_path):
    session_factory = _db()
    reference = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    run_id, _ = _build_with_stage_b_items(
        session_factory,
        reference_time=reference,
        rows=[("Current build update", 90, "candidate")],
    )
    cluster = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
        ai_client=_StageCClient(),
    )
    assert cluster.current_event_ids
    assert cluster.candidate_event_ids == cluster.current_event_ids

    stage_d = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        profile=StageDProfile(max_selected=1),
        ai_client=_SelectionClient(),
    )
    assert stage_d.selected == 1

    artifact_dir = tmp_path / "draft"
    exported = run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "public-intel",
        artifact_dir=artifact_dir,
        run_id=run_id,
    )

    assert exported.exported == 1
    assert "Current build update" in (artifact_dir / "intel_digest.md").read_text(encoding="utf-8")
    assert not (tmp_path / "public-intel" / "daily" / "2026-08-15").exists()


def test_stage_c_rerun_removes_stale_stage_d_and_export_state(tmp_path):
    session_factory = _db()
    reference = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    run_id, _ = _build_with_stage_b_items(
        session_factory,
        reference_time=reference,
        rows=[("Current build update", 90, "candidate")],
    )
    run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
        ai_client=_StageCClient(),
    )
    run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        profile=StageDProfile(max_selected=1),
        ai_client=_SelectionClient(),
    )
    run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "public-intel",
        artifact_dir=tmp_path / "draft",
        run_id=run_id,
    )

    with session_factory() as session:
        repo = IntelRepository(session)
        assert repo.get_stage(run_id, "stage_d") is not None
        assert repo.get_stage(run_id, "export") is not None

    run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
        force=True,
        ai_client=_StageCClient(),
    )

    with session_factory() as session:
        repo = IntelRepository(session)
        assert repo.get_stage(run_id, "stage_d") is None
        assert repo.get_stage(run_id, "export") is None


def test_failed_stage_c_rerun_still_removes_stale_stage_d_and_export_state(tmp_path):
    session_factory = _db()
    reference = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    run_id, _ = _build_with_stage_b_items(
        session_factory,
        reference_time=reference,
        rows=[("Current build update", 90, "candidate")],
    )
    run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=reference,
        ai_client=_StageCClient(),
    )
    run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        profile=StageDProfile(max_selected=1),
        ai_client=_SelectionClient(),
    )
    run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "public-intel",
        artifact_dir=tmp_path / "draft",
        run_id=run_id,
    )

    with pytest.raises(RuntimeError, match="stage c provider failed"):
        run_event_cluster_job(
            session_factory=session_factory,
            run_id=run_id,
            reference_time=reference,
            force=True,
            ai_client=_FailingStageCClient(),
        )

    with session_factory() as session:
        repo = IntelRepository(session)
        assert repo.get_stage(run_id, "stage_d") is None
        assert repo.get_stage(run_id, "export") is None
