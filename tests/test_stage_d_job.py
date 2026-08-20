from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.ai.skills.stage_d_editorial import strict_parse_stage_d_editorial
from app.domain.models import FetchItem, SourceSpec
from app.jobs.stage_d_job import StageDExecutionError, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEventStageDSnapshot, IntelItem
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _build_with_event(session_factory) -> int:
    source = SourceSpec(
        id="stage-d-source",
        name="Stage D source",
        transport="feed",
        url="https://example.test/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_subtype="fixed_news",
        source_role="official",
        content_class="official_model_company",
    )
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        _, build = repo.start_daily_build(edition_date="2026-08-19", reference_time=now)
        item_result = repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="stage-d-item",
                title="模型发布了新的企业能力",
                url="https://example.test/model-update",
                summary="官方发布企业能力与部署更新。",
                published_at=now,
                captured_at=now,
            ),
            run_id=build.id,
        )
        item = session.get(IntelItem, item_result.item_id)
        assert item is not None
        session.add(
            AIItemReview(
                item_id=item.id,
                content_class=source.content_class,
                topic="model_release",
                topics_json='["model_release"]',
                summary_cn="官方发布企业能力与部署更新。",
                selection_score=90,
                score_components_json='{"total":90}',
                status="success",
            )
        )
        event = repo.upsert_event(
            run_id=build.id,
            event_key="url:https://example.test/model-update",
            canonical_url="https://example.test/model-update",
            title="模型发布了新的企业能力",
            summary_cn="官方发布企业能力与部署更新。",
            topic="model_release",
            topics=["model_release"],
            content_class=source.content_class,
            source_group=source.source_group,
            source_ids=[source.id],
            source_groups=[source.source_group],
            display_score=90,
            primary_item_id=item.id,
            first_seen_at=now,
            last_seen_at=now,
        )
        repo.upsert_event_item(event.id, item.id, source_id=source.id, source_group=source.source_group, is_primary=True)
        cluster = repo.ensure_stage(build.id, "cluster")
        task = repo.ensure_stage_task(cluster, subject_type="run", subject_id=build.id, target_run_id=build.id)
        repo.complete_stage_task(task, result={"current_event_ids": [event.id]})
        repo.finish_stage(cluster, status="succeeded")
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id)


class _Client:
    model = "stage-d-test"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[list[int]] = []

    def select_events(self, events, *, edition, total_max, watchlist_max):
        if self.fail:
            raise RuntimeError("editorial unavailable")
        ids = [int(event["event_id"]) for event in events]
        self.calls.append(ids)
        return {
            "schema_version": "stage_d_editorial_v3",
            "decisions": [
                {
                    "event_id": event_id,
                    "decision": "selected",
                    "display_order": index,
                    "editorial_score": 90,
                    "story_family_id": f"story-{index}",
                    "family_position": 1,
                    "display_title_zh": "模型发布企业部署新能力",
                    "title_supporting_fields": ["title", "summary_cn"],
                    "reason_codes": ["material_change"],
                    "editorial_reason": "变化明确。",
                    "confidence": 90,
                }
                for index, event_id in enumerate(ids, start=1)
            ],
        }


def test_stage_d_schema_v3_requires_exact_coverage_and_nonselected_no_order():
    parsed = strict_parse_stage_d_editorial(
        {
            "schema_version": "stage_d_editorial_v3",
            "decisions": [
                {
                    "event_id": 1,
                    "decision": "watchlist",
                    "editorial_score": 70,
                    "story_family_id": "family-1",
                    "family_position": None,
                    "reason_codes": ["low_impact"],
                    "editorial_reason": "保留观察。",
                    "confidence": 80,
                },
                {
                    "event_id": 2,
                    "decision": "omitted",
                    "editorial_score": 20,
                    "story_family_id": "family-2",
                    "family_position": None,
                    "reason_codes": ["low_novelty"],
                    "editorial_reason": "本期不展示。",
                    "confidence": 80,
                },
            ],
        },
        event_ids=[1, 2],
    )
    assert [row.decision for row in parsed.decisions] == ["watchlist", "omitted"]
    assert all(row.display_order is None for row in parsed.decisions)
    with pytest.raises(ValueError, match="stage_d_editorial_v3"):
        strict_parse_stage_d_editorial({"schema_version": "stage_d_editorial_v2", "decisions": []}, event_ids=[])


def test_stage_d_persists_private_build_rows_only():
    session_factory = _db()
    run_id = _build_with_event(session_factory)
    client = _Client()

    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert result.selected == 1
    assert client.calls == [[1]]
    with session_factory() as session:
        rows = session.query(IntelEventStageDSnapshot).all()
        assert len(rows) == 1
        assert rows[0].run_id == run_id
        assert rows[0].selected is True
        assert "snapshot_key" not in rows[0].metadata_json
        stage = IntelRepository(session).get_stage(run_id, "stage_d")
        assert stage is not None
        assert [task.subject_type for task in stage.tasks] == ["run"]


def test_stage_d_failure_does_not_create_a_local_fallback_report():
    session_factory = _db()
    run_id = _build_with_event(session_factory)

    with pytest.raises(StageDExecutionError, match="editorial"):
        run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=_Client(fail=True))

    with session_factory() as session:
        assert session.query(IntelEventStageDSnapshot).count() == 0
