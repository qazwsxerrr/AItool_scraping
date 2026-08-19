from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.domain.models import FetchItem
from app.jobs.stage_d_job import StageDExecutionError, StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEventStageDSnapshot, IntelItem, Source
from app.storage.repository import IntelRepository


REFERENCE = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _run_with_events(session_factory, count: int = 3) -> tuple[int, list[int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        session.add(
            Source(
                id="stage-d-source",
                name="Stage D test source",
                transport="feed",
                url="https://example.test/feed.xml",
                source_group="official_blog",
                content_class="official_model_company",
            )
        )
        session.flush()
        _, build = repo.start_daily_build(edition_date="2026-08-19", reference_time=REFERENCE)
        event_ids: list[int] = []
        for index in range(count):
            inserted = repo.insert_item(
                FetchItem(
                    source_id="stage-d-source",
                    external_id=f"stage-d-{index}",
                    title=f"模型更新 {index}",
                    url=f"https://example.test/event/{index}",
                    summary=f"模型发布第 {index} 项明确更新，包含开发者可用的新能力。",
                    content_class="official_model_company",
                    published_at=REFERENCE,
                    captured_at=REFERENCE,
                ),
                run_id=build.id,
            )
            assert inserted.item_id is not None
            item = session.get(IntelItem, inserted.item_id)
            assert item is not None
            item.status = "candidate"
            item.selection_score = 90 - index
            topic = "model" if index < 2 else "product"
            session.add(
                AIItemReview(
                    item_id=item.id,
                    content_class="official_model_company",
                    topic=topic,
                    topics_json=json.dumps([topic]),
                    summary_cn=item.summary,
                    selection_score=90 - index,
                    status="success",
                )
            )
            event = repo.upsert_event(
                run_id=build.id,
                event_key=f"url:{item.canonical_url}",
                canonical_url=item.canonical_url,
                title=item.title,
                summary_cn=item.summary,
                topic=topic,
                topics=[topic],
                entities=[],
                content_class=item.content_class,
                source_group="official_blog",
                source_ids=[item.source_id],
                source_groups=["official_blog"],
                display_score=90 - index,
                novelty_status="new",
                primary_item_id=item.id,
                first_seen_at=REFERENCE,
                last_seen_at=REFERENCE,
            )
            repo.upsert_event_item(
                event.id,
                item.id,
                source_id=item.source_id,
                source_group="official_blog",
                is_primary=True,
            )
            event_ids.append(int(event.id))
        cluster = repo.ensure_stage(build.id, "cluster")
        task = repo.ensure_stage_task(cluster, subject_type="run", subject_id=build.id, target_run_id=build.id)
        repo.complete_stage_task(task, result={"current_event_ids": event_ids})
        repo.finish_stage(cluster, status="succeeded")
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id), event_ids


def _assessment(event_id: int, *, score: int = 80, must: bool = False) -> dict:
    return {
        "event_id": event_id,
        "material_change": score,
        "impact": score,
        "reader_value": score,
        "actionability": score,
        "source_support": score,
        "freshness": score,
        "must_consider": must,
        "reason_codes": ["material_change"],
        "assessment_reason": "有明确变化和读者价值。",
        "confidence": 90,
    }


def _decision(event_id: int, *, kind: str, order: int = 1, family: str | None = None) -> dict:
    if kind == "omitted":
        return {
            "event_id": event_id,
            "decision": "omitted",
            "display_order": None,
            "editorial_score": 30,
            "story_family_id": family or f"family-{event_id}",
            "family_position": None,
            "display_title_zh": None,
            "title_supporting_fields": [],
            "reason_codes": ["low_impact"],
            "editorial_reason": "本期组合名额有限。",
            "confidence": 80,
        }
    return {
        "event_id": event_id,
        "decision": kind,
        "display_order": order,
        "editorial_score": 90,
        "story_family_id": family or f"family-{event_id}",
        "family_position": 1,
        "display_title_zh": "模型更新带来开发者新能力",
        "title_supporting_fields": ["title", "summary_cn"],
        "reason_codes": ["material_change"],
        "editorial_reason": "变化明确，适合进入日报组合。",
        "confidence": 90,
    }


class _PhasedClient:
    model = "test-stage-d-v2"
    max_retries = 0

    def __init__(self, *, assessment_scores=None, composition=None, assessment_error=None, composition_error=None):
        self.assessment_scores = assessment_scores or {}
        self.composition = composition
        self.assessment_error = assessment_error
        self.composition_error = composition_error
        self.assessment_calls: list[list[int]] = []
        self.composition_calls: list[list[int]] = []

    def assess_events(self, events, *, edition):
        if self.assessment_error:
            raise self.assessment_error
        ids = [int(event["event_id"]) for event in events]
        self.assessment_calls.append(ids)
        return {
            "schema_version": "stage_d_assessment_v1",
            "assessments": [_assessment(event_id, **self.assessment_scores.get(event_id, {})) for event_id in ids],
        }

    def compose_events(self, events, *, edition, total_max, watchlist_max):
        if self.composition_error:
            raise self.composition_error
        ids = [int(event["event_id"]) for event in events]
        self.composition_calls.append(ids)
        if self.composition is not None:
            return {"schema_version": "stage_d_editorial_v2", "decisions": [self.composition(event_id) for event_id in ids]}
        return {
            "schema_version": "stage_d_editorial_v2",
            "decisions": [_decision(event_id, kind="selected", order=index + 1) for index, event_id in enumerate(ids)],
        }


class _ConcurrentAssessmentClient(_PhasedClient):
    def __init__(self) -> None:
        super().__init__()
        self.barrier = threading.Barrier(2)
        self.assessment_threads: set[int] = set()

    def assess_events(self, events, *, edition):
        self.assessment_threads.add(threading.get_ident())
        self.barrier.wait(timeout=3)
        return super().assess_events(events, edition=edition)


def test_d1_batch_reuse_skips_provider_on_second_run():
    session_factory = _db()
    run_id, _ = _run_with_events(session_factory, count=3)
    client = _PhasedClient()

    first = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)
    second = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert first.assessed == second.assessed == 3
    assert len(client.assessment_calls) == 1
    assert len(client.composition_calls) == 1
    with session_factory() as session:
        stage = IntelRepository(session).get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "succeeded"
        assert len([task for task in stage.tasks if task.subject_type == "batch"]) == 1
        assert len([task for task in stage.tasks if task.subject_type == "run"]) == 1


def test_d1_runs_two_assessment_batches_concurrently():
    session_factory = _db()
    run_id, _ = _run_with_events(session_factory, count=25)
    client = _ConcurrentAssessmentClient()

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        profile=StageDProfile(assessment_batch_size=24, assessment_concurrency=2),
    )

    assert result.assessment_batches == 2
    assert len(client.assessment_calls) == 2
    assert len(client.assessment_threads) == 2


def test_d1_failure_blocks_d3_and_keeps_current_draft_snapshot():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory, count=2)
    with session_factory() as session:
        session.add(
            IntelEventStageDSnapshot(
                run_id=run_id,
                event_id=event_ids[0],
                display_order=1,
                display_score=90,
                selected=True,
                metadata_json=json.dumps({"editorial_tier": "selected"}),
            )
        )
        session.commit()
    client = _PhasedClient(assessment_error=ValueError("assessment down"))

    with pytest.raises(StageDExecutionError, match="assessment"):
        run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert client.composition_calls == []
    with session_factory() as session:
        rows = list(session.scalars(select(IntelEventStageDSnapshot).where(IntelEventStageDSnapshot.run_id == run_id)))
        assert [row.event_id for row in rows] == [event_ids[0]]
        assert IntelRepository(session).get_stage(run_id, "stage_d").status == "failed"


def test_d3_failure_keeps_current_draft_snapshot():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory, count=1)
    with session_factory() as session:
        session.add(
            IntelEventStageDSnapshot(
                run_id=run_id,
                event_id=event_ids[0],
                display_order=1,
                display_score=90,
                selected=True,
                metadata_json=json.dumps({"old": True}),
            )
        )
        session.commit()
    client = _PhasedClient(composition_error=ValueError("composition down"))

    with pytest.raises(StageDExecutionError, match="composition"):
        run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    with session_factory() as session:
        row = session.scalar(select(IntelEventStageDSnapshot).where(IntelEventStageDSnapshot.run_id == run_id))
        assert row is not None and json.loads(row.metadata_json)["old"] is True
        assert IntelRepository(session).get_stage(run_id, "stage_d").status == "failed"


def test_d2_shortlist_obeys_cap_and_must_consider_priority():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory, count=3)
    client = _PhasedClient(assessment_scores={event_ids[2]: {"score": 20, "must": True}})

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        profile=StageDProfile(shortlist_max=2),
    )

    assert result.shortlist_count == 2
    assert len(client.composition_calls) == 1
    assert event_ids[2] in client.composition_calls[0]
    assert len(client.composition_calls[0]) == 2


def test_success_persists_selected_watchlist_and_omitted_rows():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory, count=3)

    def compose(event_id: int):
        kind = {event_ids[0]: "selected", event_ids[1]: "watchlist", event_ids[2]: "omitted"}[event_id]
        return _decision(event_id, kind=kind, order=1 if kind != "omitted" else 0)

    client = _PhasedClient(composition=compose)
    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert result.selected == 1
    assert result.watchlist == 1
    assert result.omitted == 1
    with session_factory() as session:
        rows = list(session.scalars(select(IntelEventStageDSnapshot).where(IntelEventStageDSnapshot.run_id == run_id)))
        tiers = {row.event_id: json.loads(row.metadata_json)["editorial_tier"] for row in rows}
        assert tiers == {event_ids[0]: "selected", event_ids[1]: "watchlist", event_ids[2]: "omitted"}
        assert IntelRepository(session).get_stage(run_id, "stage_d").status == "succeeded"
