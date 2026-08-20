from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import pytest
from sqlalchemy import select

from app.ai.skills.stage_c_aggregation import (
    STAGE_C_SCHEMA_VERSION,
    StageCAggregationCallResult,
    StageCAggregationClient,
    StageCAggregationProviderError,
    StageCAggregationResponse,
)
from app.domain.models import FetchItem
from app.jobs.event_cluster_job import (
    canonical_event_key,
    normalize_event_title,
    run_event_cluster_job,
)
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIItemReview,
    DailyEditionReportEntry,
    IntelEvent,
    IntelEventItem,
    IntelRunStage,
    IntelRunStageTask,
    Source,
)
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


class _AggregationClient:
    model = "stage-c-test"

    def __init__(
        self,
        responder: Callable[[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], dict[str, Any]],
    ) -> None:
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def aggregate(self, current_items, *, recent_history, edition):
        self.calls.append(
            {
                "current_items": list(current_items),
                "recent_history": list(recent_history),
                "edition": dict(edition),
            }
        )
        raw = self.responder(current_items, recent_history)
        return StageCAggregationCallResult(
            parsed=StageCAggregationResponse.model_validate(raw),
            raw_response=raw,
            request_metadata={"model": self.model, "call_count": len(self.calls)},
        )


class _FailingAggregationClient:
    model = "stage-c-test"

    def aggregate(self, current_items, *, recent_history, edition):
        raise RuntimeError("provider unavailable")


class _HttpResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class _HttpClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def post(self, url, *, headers, json, timeout):
        self.calls += 1
        return _HttpResponse(self.payload)


def _new_clusters(current_items, _history):
    return {
        "schema_version": STAGE_C_SCHEMA_VERSION,
        "clusters": [
            {
                "title_zh": str(item["title"]),
                "summary_zh": str(item.get("summary_cn") or item["title"]),
                "primary_item_id": int(item["id"]),
                "members": [{"item_id": int(item["id"]), "relation": "primary"}],
                "novelty_status": "new",
                "prior_event_key": None,
            }
            for item in current_items
        ],
    }


def _merge_all(current_items, _history):
    primary = current_items[0]
    return {
        "schema_version": STAGE_C_SCHEMA_VERSION,
        "clusters": [
            {
                "title_zh": "模型发布汇总",
                "summary_zh": "多条来源共同描述同一模型发布事件。",
                "primary_item_id": int(primary["id"]),
                "members": [
                    {
                        "item_id": int(item["id"]),
                        "relation": "primary" if index == 0 else "duplicate",
                    }
                    for index, item in enumerate(current_items)
                ],
                "novelty_status": "new",
                "prior_event_key": None,
            }
        ],
    }


def _repeat_first(current_items, history):
    item = current_items[0]
    return {
        "schema_version": STAGE_C_SCHEMA_VERSION,
        "clusters": [
            {
                "title_zh": "模型发布重复消息",
                "summary_zh": "该消息与前一日已发布事件相同，没有实质更新。",
                "primary_item_id": int(item["id"]),
                "members": [{"item_id": int(item["id"]), "relation": "primary"}],
                "novelty_status": "repeat",
                "prior_event_key": str(history[0]["event_key"]),
            }
        ],
    }


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _seed_daily_build(session_factory, *, edition_date: str, rows: list[dict[str, Any]]) -> tuple[int, list[int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        for source_id in sorted({row["source_id"] for row in rows}):
            session.add(
                Source(
                    id=source_id,
                    name=source_id,
                    transport="feed",
                    url=f"https://{source_id}.example/feed.xml",
                    source_group="official_blog",
                    content_class="official_model_company",
                )
            )
        session.flush()
        _, build = repo.start_daily_build(edition_date=edition_date, reference_time=NOW)
        item_ids: list[int] = []
        for row in rows:
            inserted = repo.insert_item(
                FetchItem(
                    source_id=row["source_id"],
                    external_id=row.get("external_id"),
                    url=row.get("url"),
                    title=row["title"],
                    summary=row.get("summary") or row["title"],
                    content_class="official_model_company",
                    published_at=NOW,
                    captured_at=NOW,
                ),
                run_id=build.id,
            )
            assert inserted.item_id is not None
            item_ids.append(int(inserted.item_id))
            session.add(
                AIItemReview(
                    item_id=int(inserted.item_id),
                    content_class="official_model_company",
                    topic="model_release",
                    topics_json=json.dumps(["model_release"]),
                    keywords_json=json.dumps(["model", "release"]),
                    entities_json="[]",
                    summary_cn=row.get("summary") or row["title"],
                    selection_score=int(row.get("score", 80)),
                    status="success",
                )
            )
        repo.freeze_run_scope(build.id)
        stage = repo.ensure_stage(build.id, "analyze")
        for item_id, row in zip(item_ids, rows, strict=True):
            task = repo.ensure_stage_task(stage, subject_type="item", subject_id=item_id, item_id=item_id)
            repo.complete_stage_task(
                task,
                result={
                    "item_id": item_id,
                    "filtered": bool(row.get("task_filtered", False)),
                    **(
                        {"analysis_filtered_reason": "stage_b_guard"}
                        if row.get("task_filtered", False)
                        else {}
                    ),
                },
            )
        repo.finish_stage(stage, status="succeeded")
        session.commit()
        return int(build.id), item_ids


def _publish_prior_report(session_factory, *, edition_date: str, event_key: str, url: str, title: str) -> None:
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.replace_published_daily_report(
            edition_date=edition_date,
            records=[
                {
                    "event_key": event_key,
                    "title": title,
                    "original_title": title,
                    "summary_cn": title,
                    "url": url,
                    "display_score": 80,
                    "topic": "model_release",
                    "content_class": "official_model_company",
                    "source_group": "official_blog",
                    "source_ids": ["prior-source"],
                    "source_refs": [],
                }
            ],
        )
        session.commit()


def test_identity_helpers_are_stable():
    assert normalize_event_title("  Model—Release  v1.0  ") == "model release v1 0"
    assert canonical_event_key({"url": "https://example.test/a/?utm_medium=x"}) == "url:https://example.test/a"
    assert canonical_event_key({"external_id": " GUID 42 "}) == "external:guid42"


def test_stage_c_reads_score_eligible_stage_b_items_once_and_persists_sources():
    session_factory = _db()
    run_id, item_ids = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {"source_id": "official-a", "external_id": "release-1", "url": "https://example.test/release-1", "title": "Model release"},
            {"source_id": "official-b", "external_id": "release-1-copy", "url": "https://media.test/release-1", "title": "Model release recap"},
        ],
    )
    client = _AggregationClient(_merge_all)

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        ai_client=client,
    )

    assert len(client.calls) == 1
    assert {int(row["id"]) for row in client.calls[0]["current_items"]} == set(item_ids)
    assert result.processed == 2
    assert result.events == 1
    assert result.merged == 1
    with session_factory() as session:
        event = session.scalar(select(IntelEvent))
        assert event is not None and event.build_id == run_id
        relations = session.scalars(select(IntelEventItem).order_by(IntelEventItem.item_id)).all()
        assert [relation.item_id for relation in relations] == sorted(item_ids)
        assert [relation.match_type for relation in relations] == ["primary", "duplicate"]
        assert {relation.source_id for relation in relations} == {"official-a", "official-b"}


def test_stage_c_applies_score_and_structural_input_guards():
    session_factory = _db()
    run_id, item_ids = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {
                "source_id": "official-a",
                "external_id": "release-1",
                "url": "https://example.test/release-1",
                "title": "Event seed at threshold",
                "score": 60,
            },
            {
                "source_id": "official-a",
                "external_id": "release-2",
                "url": "https://example.test/release-2",
                "title": "Supporting evidence at threshold",
                "score": 60,
            },
            {
                "source_id": "official-a",
                "external_id": "release-3",
                "url": "https://example.test/release-3",
                "title": "Below threshold",
                "score": 59,
            },
            {
                "source_id": "official-a",
                "external_id": "release-4",
                "url": "https://example.test/release-4",
                "title": "Another threshold event",
                "score": 90,
            },
            {
                "source_id": "official-a",
                "external_id": "release-5",
                "url": "https://example.test/release-5",
                "title": "Structurally filtered",
                "score": 90,
                "task_filtered": True,
            },
        ],
    )
    client = _AggregationClient(_new_clusters)

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        ai_client=client,
    )

    assert result.processed == 3
    assert {int(row["id"]) for row in client.calls[0]["current_items"]} == {item_ids[index] for index in (0, 1, 3)}
    assert result.input_audit["min_score"] == 60
    assert result.input_audit["excluded_counts"] == {
        "analysis_filtered": 1,
        "below_min_score": 1,
        "missing_item": 0,
        "missing_review": 0,
    }


def test_stage_c_threshold_change_invalidates_the_aggregation_task():
    session_factory = _db()
    run_id, _ = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {
                "source_id": "official-a",
                "external_id": "release-1",
                "url": "https://example.test/release-1",
                "title": "Threshold-sensitive release",
                "score": 80,
            }
        ],
    )
    client = _AggregationClient(_new_clusters)

    run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        ai_client=client,
        input_min_score=60,
    )
    rerun = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        ai_client=client,
        input_min_score=70,
    )

    assert len(client.calls) == 2
    assert rerun.input_audit["min_score"] == 70
    with session_factory() as session:
        stage = session.scalar(
            select(IntelRunStage).where(
                IntelRunStage.run_id == run_id,
                IntelRunStage.stage_name == "cluster",
            )
        )
        task = session.scalar(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id))
        assert stage is not None and stage.metadata_dict["input_min_score"] == 70
        assert task is not None and task.result["input_audit"]["min_score"] == 70


def test_stage_c_waits_for_incomplete_stage_b_items():
    session_factory = _db()
    run_id, item_ids = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {"source_id": "official-a", "external_id": "release-1", "url": "https://example.test/release-1", "title": "Model release"}
        ],
    )
    with session_factory() as session:
        analyze = session.scalar(
            select(IntelRunStage).where(
                IntelRunStage.run_id == run_id,
                IntelRunStage.stage_name == "analyze",
            )
        )
        task = session.scalar(
            select(IntelRunStageTask).where(
                IntelRunStageTask.stage_id == analyze.id,
                IntelRunStageTask.subject_type == "item",
                IntelRunStageTask.item_id == item_ids[0],
            )
        )
        assert task is not None
        task.status = "pending"
        analyze.status = "pending"
        session.commit()

    with pytest.raises(ValueError, match="Stage B analysis stage to finish"):
        run_event_cluster_job(
            session_factory=session_factory,
            run_id=run_id,
            now=NOW,
            ai_client=_AggregationClient(_new_clusters),
        )


def test_stage_c_uses_ai_history_decision_and_creates_a_fresh_build_row():
    session_factory = _db()
    url = "https://example.test/release-1"
    event_key = f"url:{url}"
    _publish_prior_report(
        session_factory,
        edition_date="2026-08-18",
        event_key=event_key,
        url=url,
        title="Yesterday model release",
    )
    run_id, _ = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {"source_id": "official-a", "external_id": "release-1", "url": url, "title": "Today model release"}
        ],
    )
    client = _AggregationClient(_repeat_first)

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        ai_client=client,
    )

    assert client.calls[0]["recent_history"][0]["event_key"] == event_key
    assert result.events == 0
    assert result.repeats == 1
    assert len(result.current_event_ids) == 1
    with session_factory() as session:
        entry = session.scalar(select(DailyEditionReportEntry))
        event = session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id))
        assert entry is not None and entry.event_key == event_key
        assert event is not None and event.event_key == event_key
        assert event.novelty_status == "repeat"


def test_stage_c_force_replaces_the_previous_ai_partition():
    session_factory = _db()
    run_id, item_ids = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {"source_id": "official-a", "external_id": "release-1", "url": "https://example.test/release-1", "title": "Model release"},
            {"source_id": "official-b", "external_id": "release-2", "url": "https://example.test/release-2", "title": "Tool release"},
        ],
    )

    first = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        ai_client=_AggregationClient(_merge_all),
    )
    second = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        now=NOW,
        force=True,
        ai_client=_AggregationClient(_new_clusters),
    )

    assert len(first.current_event_ids) == 1
    assert len(second.current_event_ids) == 2
    with session_factory() as session:
        events = session.scalars(select(IntelEvent).order_by(IntelEvent.id)).all()
        relations = session.scalars(select(IntelEventItem).order_by(IntelEventItem.item_id)).all()
        assert len(events) == 2
        assert {relation.item_id for relation in relations} == set(item_ids)
        assert len({relation.event_id for relation in relations}) == 2


def test_stage_c_provider_failure_is_not_repaired_or_fallbacked():
    session_factory = _db()
    run_id, _ = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {"source_id": "official-a", "external_id": "release-1", "url": "https://example.test/release-1", "title": "Model release"}
        ],
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        run_event_cluster_job(
            session_factory=session_factory,
            run_id=run_id,
            now=NOW,
            ai_client=_FailingAggregationClient(),
        )

    with session_factory() as session:
        assert session.scalar(select(IntelEvent)) is None
        cluster_stage = session.scalar(
            select(IntelRunStage).where(
                IntelRunStage.run_id == run_id,
                IntelRunStage.stage_name == "cluster",
            )
        )
        task = session.scalar(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == cluster_stage.id))
        assert task is not None
        assert task.status == "blocked"
        assert task.error_code == "stage_c_ai_aggregation_failed"


def test_stage_c_http_client_calls_provider_once_and_does_not_retry_schema_errors():
    item = {
        "id": 7,
        "title": "Model release",
        "summary_cn": "A model was released.",
    }
    invalid_http = _HttpClient({"schema_version": STAGE_C_SCHEMA_VERSION, "clusters": []})
    client = StageCAggregationClient(
        api_url="https://ai.example.test",
        api_key="secret",
        model="test-model",
        api_style="generic_json",
        timeout_seconds=30,
        http_client=invalid_http,
    )

    with pytest.raises(StageCAggregationProviderError, match="missing item_ids"):
        client.aggregate([item], recent_history=[], edition={"edition_date": "2026-08-19"})

    assert invalid_http.calls == 1
