from __future__ import annotations

import json
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
            topic = "model_release" if index < 2 else "product_application"
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
            repo.upsert_event_item(event.id, item.id, source_id=item.source_id, source_group="official_blog", is_primary=True)
            event_ids.append(int(event.id))
        cluster = repo.ensure_stage(build.id, "cluster")
        task = repo.ensure_stage_task(cluster, subject_type="run", subject_id=build.id, target_run_id=build.id)
        repo.complete_stage_task(task, result={"current_event_ids": event_ids})
        repo.finish_stage(cluster, status="succeeded")
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id), event_ids


def _decision(event_id: int, *, kind: str, order: int = 1, family: str | None = None) -> dict:
    base = {
        "event_id": event_id,
        "decision": kind,
        "editorial_score": 90 if kind != "omitted" else 30,
        "story_family_id": family or f"family-{event_id}",
        "family_position": 1 if kind == "selected" else None,
        "reason_codes": ["material_change"] if kind == "selected" else ["low_impact"],
        "editorial_reason": "变化明确，适合进入日报组合。" if kind == "selected" else "本期组合名额有限。",
        "confidence": 90,
    }
    if kind == "selected":
        base.update(
            {
                "display_order": order,
                "display_title_zh": "模型更新带来开发者新能力",
                "title_supporting_fields": ["title", "summary_cn"],
            }
        )
    return base


class _EditorialClient:
    model = "test-stage-d-v3"
    max_retries = 0

    def __init__(self, *, decision=None, error=None):
        self.decision = decision
        self.error = error
        self.calls: list[list[int]] = []

    def select_events(self, events, *, edition, total_max, watchlist_max):
        if self.error:
            raise self.error
        ids = [int(event["event_id"]) for event in events]
        self.calls.append(ids)
        callback = self.decision or (lambda event_id, index: _decision(event_id, kind="selected", order=index))
        return {
            "schema_version": "stage_d_editorial_v3",
            "decisions": [callback(event_id, index) for index, event_id in enumerate(ids, start=1)],
        }


def test_editorial_run_task_reuse_skips_provider_on_second_run():
    session_factory = _db()
    run_id, _ = _run_with_events(session_factory, count=3)
    client = _EditorialClient()

    first = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)
    second = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert first.selected == second.selected == 3
    assert len(client.calls) == 1
    with session_factory() as session:
        stage = IntelRepository(session).get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "succeeded"
        assert [(task.subject_type, task.subject_id) for task in stage.tasks] == [("run", str(run_id))]


def test_editorial_makes_one_provider_call_for_all_candidates():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory, count=25)
    client = _EditorialClient()

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        profile=StageDProfile(total_max=10),
    )

    assert result.processed == result.eligible == 25
    assert client.calls == [event_ids]


def test_editorial_failure_keeps_current_draft_snapshot():
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
                metadata_json=json.dumps({"old": True}),
            )
        )
        session.commit()
    client = _EditorialClient(error=ValueError("editorial down"))

    with pytest.raises(StageDExecutionError, match="editorial"):
        run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert len(client.calls) == 0
    with session_factory() as session:
        row = session.scalar(select(IntelEventStageDSnapshot).where(IntelEventStageDSnapshot.run_id == run_id))
        assert row is not None and json.loads(row.metadata_json)["old"] is True
        assert IntelRepository(session).get_stage(run_id, "stage_d").status == "failed"


def test_editorial_local_limits_and_selected_watchlist_omitted_rows():
    session_factory = _db()
    run_id, event_ids = _run_with_events(session_factory, count=3)

    def decision(event_id: int, index: int):
        kind = {event_ids[0]: "selected", event_ids[1]: "watchlist", event_ids[2]: "omitted"}[event_id]
        return _decision(event_id, kind=kind, order=index)

    client = _EditorialClient(decision=decision)
    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert result.selected == 1
    assert result.watchlist == 1
    assert result.omitted == 1
    with session_factory() as session:
        rows = list(session.scalars(select(IntelEventStageDSnapshot).where(IntelEventStageDSnapshot.run_id == run_id)))
        tiers = {row.event_id: json.loads(row.metadata_json)["editorial_tier"] for row in rows}
        assert tiers == {event_ids[0]: "selected", event_ids[1]: "watchlist", event_ids[2]: "omitted"}
        display_orders = {row.event_id: row.display_order for row in rows}
        assert display_orders[event_ids[1]] > 1  # local watchlist order follows selected cards
