from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.ai.responses import AgentRunResult
from app.ai.skills.intel_triage import ScreenResult
from app.domain.models import FetchItem, SourceSpec
from app.domain.recency import (
    stage_a_freshness_scope,
    stage_a_cutoff_at,
    stage_a_time_decision,
)
from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs import pipeline_orchestrator
from app.jobs.stage_a_screen_job import run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import run_stage_b_analysis_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIItemReview,
    IntelEvent,
    IntelEventItem,
    IntelItem,
    IntelRunItem,
    IntelRunStageTask,
)
from app.storage.repository import IntelRepository


REFERENCE = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)


def _factory(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'recent-window.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _feed_source() -> SourceSpec:
    return SourceSpec(
        id="recent-window-feed",
        name="Recent Window Feed",
        transport="feed",
        url="https://example.test/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        content_class="official_model_company",
    )


def test_stage_a_cutoff_uses_previous_day_midnight_in_shanghai():
    cutoff = stage_a_cutoff_at("2026-08-22")
    assert cutoff == datetime(2026, 8, 20, 16, tzinfo=timezone.utc)

    source = _feed_source()
    exact_boundary = SimpleNamespace(published_at=cutoff)
    just_before = SimpleNamespace(published_at=cutoff - timedelta(seconds=1))
    assert stage_a_time_decision(
        exact_boundary,
        source=source,
        reference_time=datetime(2026, 8, 22, 8, tzinfo=timezone.utc),
        edition_date="2026-08-22",
    ).eligible is True
    assert stage_a_time_decision(
        just_before,
        source=source,
        reference_time=datetime(2026, 8, 22, 8, tzinfo=timezone.utc),
        edition_date="2026-08-22",
    ).reason == "too_old"

    trending = SourceSpec(
        id="recent-window-trending",
        name="Recent Window Trending",
        transport="github",
        url="https://github.com/trending",
        github={"mode": "trending", "period": "daily"},
        content_class="project_tool",
    )
    trending_item = SimpleNamespace(
        published_at=None,
        captured_at=datetime(2026, 8, 22, 9, tzinfo=timezone.utc),
    )
    decision = stage_a_time_decision(
        trending_item,
        source=trending,
        reference_time=datetime(2026, 8, 22, 8, tzinfo=timezone.utc),
        edition_date="2026-08-22",
    )
    assert decision.eligible is True
    assert decision.reason == "trending_exempt"
    assert decision.time_basis == "captured_at_discovery"


class _ScreenProvider:
    model = "recent-window-fixture"

    def __init__(self):
        self.titles: list[str] = []

    def screen(self, envelope):
        self.titles.append(envelope.title)
        return ScreenResult(
            item_id=envelope.item_id,
            decision="pass",
            reason_code="relevant",
            reason="fixture pass",
            confidence=95,
            risk_flags=[],
            raw_response={"fixture": "screen"},
        )


class _StageCClient:
    model = "recent-window-stage-c"
    transport = "responses"

    def run(self, *, function_tools, on_response, on_tool, **_kwargs):
        tools = {tool.name: tool for tool in function_tools}
        on_response(1, {"id": "recent-c", "output": []})
        calls = 0

        def invoke(name, args):
            nonlocal calls
            calls += 1
            call = {"name": name, "call_id": f"call-{calls}", "arguments": json.dumps(args)}
            output = dict(tools[name].handler(args))
            on_tool(1, call, output)
            return output

        rows = invoke("list_candidates", {"bucket": "active", "offset": 0, "limit": 30})["items"]
        invoke("save_event_drafts", {"drafts": [
            {
                "draft_key": f"recent-{item['id']}", "item_ids": [item["id"]], "title": item["title"],
                "summary_cn": item.get("summary_cn") or item["title"], "topic": item.get("topic") or "technology_insight",
                "topics": [item.get("topic") or "technology_insight"], "keywords": item.get("keywords") or [],
                "entities": item.get("entities") or [], "novelty_status": "new", "prior_event_key": None,
                "review_state": "candidate", "confidence": 90, "risk_flags": [],
            }
            for item in rows
        ]})
        assert invoke("finalize_event_drafts", {})["ok"] is True
        return AgentRunResult("recent-c", 1, calls, 0, True, {"id": "recent-c", "output": []})


def test_stage_a_does_not_time_filter_github_trending_projects(tmp_path):
    session_factory = _factory(tmp_path)
    source = SourceSpec(
        id="recent-window-trending",
        name="Recent Window Trending",
        transport="github",
        url="https://github.com/trending",
        github={"mode": "trending", "period": "daily"},
        content_class="project_tool",
    )
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source)
        _, run = repo.start_daily_build(
            edition_date="2026-08-16",
            reference_time=REFERENCE,
            scope=stage_a_freshness_scope(),
        )
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="github_repo:example/trending-project",
                title="GitHub repo: example/trending-project",
                url="https://github.com/example/trending-project",
                summary="A current GitHub Trending project.",
                content_class=source.content_class,
                # Deliberately old: Trending must be admitted independently
                # of any publication/activity timestamp.
                published_at=REFERENCE - timedelta(days=365),
                captured_at=REFERENCE + timedelta(minutes=1),
            ),
            run_id=run.id,
        )
        repo.freeze_run_scope(run.id)
        session.commit()
        run_id = int(run.id)

    provider = _ScreenProvider()
    result = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        limit=None,
    )

    assert provider.titles == ["GitHub repo: example/trending-project"]
    assert result.processed == 1
    assert result.time_filtered == 0


def test_stage_a_keeps_only_recent_items_and_records_run_local_audit(tmp_path):
    session_factory = _factory(tmp_path)
    source = _feed_source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source)
        _, run = repo.start_daily_build(
            edition_date="2026-08-16",
            reference_time=REFERENCE,
            scope=stage_a_freshness_scope(),
        )
        cutoff = stage_a_cutoff_at("2026-08-16")
        for index, (title, published_at) in enumerate(
            (
                ("fresh boundary", cutoff),
                ("within rolling 72h but before edition cutoff", REFERENCE - timedelta(hours=48)),
                ("stale", cutoff - timedelta(seconds=1)),
                ("undated", None),
                ("future", REFERENCE + timedelta(seconds=1)),
            ),
            start=1,
        ):
            repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=f"recent-window:{index}",
                    title=title,
                    url=f"https://example.test/{index}",
                    summary=title,
                    content_class=source.content_class,
                    published_at=published_at,
                    captured_at=REFERENCE,
                ),
                run_id=run.id,
            )
        repo.freeze_run_scope(run.id)
        session.commit()
        run_id = int(run.id)

    provider = _ScreenProvider()
    result = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        limit=None,
    )

    assert provider.titles == ["fresh boundary"]
    assert result.processed == 1
    assert result.edition_date == "2026-08-16"
    assert result.cutoff_at == cutoff
    assert result.time_filtered == 4
    assert result.time_filter_counts == {
        "too_old": 2,
        "missing_published_at": 1,
        "future_timestamp": 1,
    }
    with session_factory() as session:
        statuses = dict(
            session.execute(
                select(IntelItem.title, IntelRunItem.status)
                .join(IntelRunItem, IntelRunItem.item_id == IntelItem.id)
                .where(IntelRunItem.run_id == run_id)
            ).all()
        )
        assert statuses == {
            "fresh boundary": "new",
            "within rolling 72h but before edition cutoff": "time_too_old",
            "stale": "time_too_old",
            "undated": "time_missing_published_at",
            "future": "time_future_timestamp",
        }
        stage = IntelRepository(session).get_stage(run_id, "screen")
        tasks = session.scalars(
            select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id)
        ).all()
        assert sorted(task.status for task in tasks) == [
            "skipped",
            "skipped",
            "skipped",
            "skipped",
            "succeeded",
        ]
        assert stage.status == "succeeded"


def test_stage_c_uses_successful_stage_b_items_without_reapplying_recency(tmp_path):
    session_factory = _factory(tmp_path)
    source = _feed_source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source)
        _, run = repo.start_daily_build(
            edition_date="2026-08-16",
            reference_time=REFERENCE,
            scope=stage_a_freshness_scope(),
        )
        analyze_stage = repo.ensure_stage(run.id, "analyze")
        item_ids: dict[str, int] = {}
        for index, (title, published_at) in enumerate(
            (
                ("recent candidate", REFERENCE - timedelta(hours=1)),
                ("stale candidate", REFERENCE - timedelta(hours=73)),
            ),
            start=1,
        ):
            inserted = repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=f"recent-window-event-{index}",
                    title=title,
                    url=f"https://example.test/recent-window-event-{index}",
                    content_class=source.content_class,
                    published_at=published_at,
                    captured_at=REFERENCE,
                ),
                run_id=run.id,
            )
            assert inserted.item_id is not None
            item = session.get(IntelItem, inserted.item_id)
            assert item is not None
            item.b1_priority = 80
            item.status = "candidate"
            item_ids[title] = int(item.id)
            session.add(
                AIItemReview(
                    item_id=item.id,
                    content_class=source.content_class,
                    topic="model_release",
                    topics_json='["model_release"]',
                    keywords_json='["release"]',
                    b1_priority=80,
                    status="success",
                )
            )
            task = repo.ensure_stage_task(
                analyze_stage,
                subject_type="item",
                subject_id=item.id,
                item_id=item.id,
            )
            repo.complete_stage_task(task, result={"item_id": item.id, "reason": "candidate"})
            # C consumes B's persisted active/reserve projection rather than
            # inferring an input set from legacy item statuses.
        repo.replace_candidate_admissions(
            run.id,
            [
                {
                    "item_id": item_id,
                    "decision": "active",
                    "rank": position,
                    "guarded_score": 80,
                    "reason_code": "fixture",
                    "reason": "fixture",
                    "policy_version": "fixture",
                    "policy_fingerprint": "fixture",
                }
                for position, item_id in enumerate(item_ids.values(), start=1)
            ],
        )
        repo.freeze_run_scope(run.id)
        repo.finish_stage(analyze_stage, status="succeeded")
        session.commit()
        run_id = int(run.id)

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=REFERENCE,
        ai_client=_StageCClient(),
    )

    assert result.processed == 2
    assert result.events == 2
    with session_factory() as session:
        relations = session.scalars(select(IntelEventItem)).all()
        assert {relation.item_id for relation in relations} == set(item_ids.values())


def test_all_time_filtered_items_advance_the_empty_pipeline_path(tmp_path):
    session_factory = _factory(tmp_path)
    source = _feed_source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source)
        _, run = repo.start_daily_build(
            edition_date="2026-08-16",
            reference_time=REFERENCE,
            scope=stage_a_freshness_scope(),
        )
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="all-stale",
                title="all stale",
                url="https://example.test/all-stale",
                summary="all stale",
                content_class=source.content_class,
                published_at=REFERENCE - timedelta(hours=73),
                captured_at=REFERENCE,
            ),
            run_id=run.id,
        )
        repo.freeze_run_scope(run.id)
        session.commit()
        run_id = int(run.id)

    provider = _ScreenProvider()
    screened = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        limit=None,
    )
    analyzed = run_stage_b_analysis_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        limit=None,
    )

    assert screened.time_filtered == 1
    assert screened.processed == 0
    assert analyzed.processed == 0
    assert provider.titles == []
    with session_factory() as session:
        stage = IntelRepository(session).get_stage(run_id, "analyze")
        assert stage is not None and stage.status == "succeeded"
    assert pipeline_orchestrator._stage_needs_resume(session_factory, run_id, "cluster") is True
