from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.ai.skills.intel_triage import ScreenResult
from app.domain.models import FetchItem, SourceSpec
from app.domain.recency import recent_window_decision, recent_window_scope
from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs.export_job import run_intel_export_job
from app.jobs import pipeline_orchestrator
from app.jobs.stage_a_screen_job import run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import run_stage_b_analysis_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    AIItemReview,
    IntelEvent,
    IntelEventItem,
    IntelEventRankingSnapshot,
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


def test_recent_window_is_inclusive_and_trending_is_exempt_from_time_filter():
    source = _feed_source()
    exact_boundary = SimpleNamespace(
        published_at=REFERENCE - timedelta(hours=72),
        captured_at=REFERENCE,
    )
    stale = SimpleNamespace(
        published_at=REFERENCE - timedelta(hours=72, seconds=1),
        captured_at=REFERENCE,
    )
    missing = SimpleNamespace(published_at=None, captured_at=REFERENCE)
    future = SimpleNamespace(published_at=REFERENCE + timedelta(seconds=1), captured_at=REFERENCE)

    assert recent_window_decision(exact_boundary, source=source, reference_time=REFERENCE).eligible is True
    assert recent_window_decision(stale, source=source, reference_time=REFERENCE).reason == "too_old"
    assert recent_window_decision(missing, source=source, reference_time=REFERENCE).reason == "missing_published_at"
    assert recent_window_decision(future, source=source, reference_time=REFERENCE).reason == "future_timestamp"

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
        # A pipeline freezes its reference before fetch, so Trending discovery
        # time naturally lands after the frozen run reference.
        captured_at=REFERENCE + timedelta(hours=1),
    )
    decision = recent_window_decision(trending_item, source=trending, reference_time=REFERENCE)
    assert decision.eligible is True
    assert decision.reason == "trending_exempt"
    assert decision.time_basis == "captured_at_discovery"
    assert decision.age_hours is None


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
        repo.upsert_source(source, policy=source)
        run = repo.start_run(reference_time=REFERENCE, scope=recent_window_scope())
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
        repo.upsert_source(source, policy=source)
        run = repo.start_run(reference_time=REFERENCE, scope=recent_window_scope())
        for index, (title, published_at) in enumerate(
            (
                ("fresh boundary", REFERENCE - timedelta(hours=72)),
                ("stale", REFERENCE - timedelta(hours=72, seconds=1)),
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
    assert result.time_filtered == 3
    assert result.time_filter_counts == {
        "too_old": 1,
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
            "stale": "time_too_old",
            "undated": "time_missing_published_at",
            "future": "time_future_timestamp",
        }
        stage = IntelRepository(session).get_stage(run_id, "screen")
        tasks = session.scalars(
            select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id)
        ).all()
        assert sorted(task.status for task in tasks) == ["skipped", "skipped", "skipped", "succeeded"]
        assert stage.status == "succeeded"


def test_stage_c_rechecks_the_frozen_recent_window(tmp_path):
    session_factory = _factory(tmp_path)
    source = _feed_source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        run = repo.start_run(reference_time=REFERENCE, scope=recent_window_scope())
        analyze_stage = repo.ensure_stage(run.id, "analyze")
        item_ids: dict[str, int] = {}
        for index, (title, published_at) in enumerate(
            (
                ("recent candidate", REFERENCE - timedelta(hours=1)),
                ("stale candidate", REFERENCE - timedelta(hours=73)),
            ),
            start=1,
        ):
            item = IntelItem(
                source_id=source.id,
                title=title,
                content_class=source.content_class,
                content_hash=(f"recent-window-event-{index}" * 8)[:64],
                selection_score=80,
                status="candidate",
                published_at=published_at,
                captured_at=REFERENCE,
            )
            session.add(item)
            session.flush()
            item_ids[title] = int(item.id)
            repo.record_run_item(run.id, item.id)
            session.add(
                AIItemReview(
                    item_id=item.id,
                    run_id=run.id,
                    content_class=source.content_class,
                    topic="model",
                    topics_json='["model"]',
                    keywords_json='["release"]',
                    selection_score=80,
                    reason="candidate",
                    status="success",
                )
            )
            task = repo.ensure_stage_task(
                analyze_stage,
                subject_type="item",
                subject_id=item.id,
                item_id=item.id,
            )
            task.status = "succeeded"
            task.result = {"item_id": item.id, "reason": "candidate"}
        session.commit()
        run_id = int(run.id)

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        reference_time=REFERENCE,
    )

    assert result.processed == 1
    assert result.events == 1
    with session_factory() as session:
        relations = session.scalars(select(IntelEventItem)).all()
        assert [relation.item_id for relation in relations] == [item_ids["recent candidate"]]


def test_all_time_filtered_items_advance_the_empty_pipeline_path(tmp_path):
    session_factory = _factory(tmp_path)
    source = _feed_source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        run = repo.start_run(reference_time=REFERENCE, scope=recent_window_scope())
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


def test_run_scoped_export_cannot_leak_a_stale_primary_item(tmp_path):
    session_factory = _factory(tmp_path)
    source = _feed_source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        run = repo.start_run(reference_time=REFERENCE, scope=recent_window_scope())
        stale = IntelItem(
            source_id=source.id,
            title="stale selected event",
            content_class=source.content_class,
            content_hash="stale-selected-event".ljust(64, "x"),
            status="candidate",
            published_at=REFERENCE - timedelta(hours=73),
            captured_at=REFERENCE,
        )
        session.add(stale)
        session.flush()
        repo.record_run_item(run.id, stale.id)
        event = IntelEvent(
            event_key="title:stale-selected-event",
            title="stale selected event",
            summary_cn="stale",
            topic="model",
            display_score=90,
            new_in_run_id=run.id,
            first_run_id=run.id,
        )
        session.add(event)
        session.flush()
        session.add(
            IntelEventItem(
                event_id=event.id,
                item_id=stale.id,
                source_id=source.id,
                is_primary=True,
            )
        )
        session.add(
            IntelEventRankingSnapshot(
                snapshot_key=f"run-{run.id}",
                run_id=run.id,
                event_id=event.id,
                rank=1,
                display_score=90,
                selected=True,
            )
        )
        session.commit()
        run_id = int(run.id)

    result = run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "intel",
        run_id=run_id,
    )

    assert result.exported == 0
    assert "保留条目：0" in (tmp_path / "runs" / f"run-{run_id}" / "intel_digest.md").read_text(encoding="utf-8")
