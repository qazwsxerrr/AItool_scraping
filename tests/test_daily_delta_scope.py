from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.ai.skills.intel_triage import ScreenResult
from app.domain.models import SourceSpec
from app.jobs.event_cluster_job import _load_history_events
from app.jobs.stage_a_screen_job import run_stage_a_screen_job
from app.jobs.stage_d_job import run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventStageDSnapshot, IntelRun, IntelRunItem
from app.storage.repository import IntelRepository
from app.storage.run_snapshot_summary import build_run_snapshot_summary


REFERENCE = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _source() -> SourceSpec:
    return SourceSpec(
        id="daily-delta-source",
        name="Daily delta source",
        transport="feed",
        url="https://example.test/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_role="official",
        content_class="official_model_company",
    )


def _item_payload(*, title: str, content_hash: str) -> dict[str, object]:
    return {
        "source_id": "daily-delta-source",
        "external_id": "article:stable-id",
        "canonical_url": "https://example.test/article",
        "title": title,
        "summary": title,
        "content_text": title,
        "published_at": REFERENCE,
        "captured_at": REFERENCE,
        "content_class": "official_model_company",
        "content_hash": content_hash,
    }


class _ScreenClient:
    model = "daily-delta-test"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def screen(self, envelope):
        self.calls.append(int(envelope.item_id))
        return ScreenResult(
            item_id=envelope.item_id,
            decision="pass",
            reason_code="fixture_pass",
            reason="fixture pass",
            confidence=95,
            risk_flags=[],
            raw_response={"fixture": "screen"},
        )


def test_only_new_or_changed_fetches_enter_stage_a():
    session_factory = _db()
    source = _source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source)
        first_run = repo.start_run(reference_time=REFERENCE, edition_date="2026-08-17")
        first = repo.insert_item(
            _item_payload(title="Initial model release", content_hash="a" * 64),
            run_id=first_run.id,
        )
        assert first.item_id is not None
        repo.set_item_status(first.item_id, "candidate", run_id=first_run.id)
        unchanged_run = repo.start_run(reference_time=REFERENCE, edition_date="2026-08-18")
        repo.insert_item(
            _item_payload(title="Initial model release", content_hash="a" * 64),
            run_id=unchanged_run.id,
        )
        session.commit()
        roles = {
            int(run_id): role
            for run_id, role in session.execute(
                select(IntelRunItem.run_id, IntelRunItem.role)
            ).all()
        }

    assert roles[int(first_run.id)] == "new"
    assert roles[int(unchanged_run.id)] == "unchanged"

    screen = _ScreenClient()
    unchanged = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=screen,
        run_id=int(unchanged_run.id),
        limit=None,
    )

    assert unchanged.processed == 0
    assert screen.calls == []
    with session_factory() as session:
        unchanged_run_row = session.get(IntelRun, int(unchanged_run.id))
        assert unchanged_run_row is not None
        summary = build_run_snapshot_summary(
            session,
            run=unchanged_run_row,
            snapshot_key=unchanged_run_row.daily_snapshot_key,
        )
        assert summary["funnel"]["frozen"] == 1
        assert summary["funnel"]["daily_delta"] == 0
        assert summary["funnel"]["within_72h"] == 0

    with session_factory() as session:
        repo = IntelRepository(session)
        changed_run = repo.start_run(reference_time=REFERENCE, edition_date="2026-08-19")
        repo.insert_item(
            _item_payload(title="Initial model release gains tool use", content_hash="b" * 64),
            run_id=changed_run.id,
        )
        session.commit()
        changed_role = session.scalar(
            select(IntelRunItem.role).where(IntelRunItem.run_id == changed_run.id)
        )

    changed = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=screen,
        run_id=int(changed_run.id),
        limit=None,
    )

    assert changed_role == "changed"
    assert changed.processed == 1
    assert screen.calls == [int(first.item_id)]


def test_stage_c_history_contains_only_prior_selected_daily_events():
    session_factory = _db()
    with session_factory() as session:
        repo = IntelRepository(session)
        previous = repo.start_run(reference_time=REFERENCE, edition_date="2026-08-18")
        previous.status = "completed"
        current = repo.start_run(reference_time=REFERENCE, edition_date="2026-08-19")
        selected = IntelEvent(
            event_key="selected-event",
            title="Selected prior event",
            first_seen_at=REFERENCE,
            last_seen_at=REFERENCE,
        )
        omitted = IntelEvent(
            event_key="omitted-event",
            title="Omitted prior event",
            first_seen_at=REFERENCE,
            last_seen_at=REFERENCE,
        )
        recent_but_unpublished = IntelEvent(
            event_key="unpublished-event",
            title="Recent unpublished event",
            first_seen_at=REFERENCE,
            last_seen_at=REFERENCE,
        )
        session.add_all([selected, omitted, recent_but_unpublished])
        session.flush()
        session.add_all(
            [
                IntelEventStageDSnapshot(
                    snapshot_key=previous.daily_snapshot_key,
                    event_id=selected.id,
                    run_id=previous.id,
                    selected=True,
                ),
                IntelEventStageDSnapshot(
                    snapshot_key=previous.daily_snapshot_key,
                    event_id=omitted.id,
                    run_id=previous.id,
                    selected=False,
                ),
            ]
        )
        session.commit()

        history = _load_history_events(
            session,
            current=REFERENCE,
            snapshot_key="run-current",
            run=current,
        )

    assert [event.id for event in history] == [selected.id]


class _EditorialClient:
    model = "daily-delta-editorial"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, object]]] = []

    def select_events(self, events, *, edition, total_max):
        self.calls.append(events)
        event_id = int(events[0]["event_id"])
        return {
            "schema_version": "stage_d_editorial_v1",
            "decisions": [
                {
                    "event_id": event_id,
                    "decision": "selected",
                    "display_order": 1,
                    "editorial_score": 90,
                    "story_family_id": "same-story",
                    "family_position": 1,
                    "display_title_zh": "测试模型发布新的能力更新",
                    "title_supporting_fields": ["title", "summary_cn"],
                    "reason_codes": ["high_reader_value"],
                    "editorial_reason": "当前事件具备展示价值。",
                    "confidence": 90,
                }
            ],
        }


def test_stage_d_suppresses_a_prior_final_output_without_material_update():
    session_factory = _db()
    with session_factory() as session:
        repo = IntelRepository(session)
        previous = repo.start_run(reference_time=REFERENCE, edition_date="2026-08-18")
        previous.status = "completed"
        current = repo.start_run(reference_time=REFERENCE, edition_date="2026-08-19")
        event = IntelEvent(
            event_key="prior-final-event",
            title="Model release update",
            summary_cn="模型发布更新，包含开发者可用的新能力。",
            topic="model",
            display_score=90,
            first_seen_at=REFERENCE,
            last_seen_at=REFERENCE,
        )
        session.add(event)
        session.flush()
        session.add(
            IntelEventStageDSnapshot(
                snapshot_key=previous.daily_snapshot_key,
                event_id=event.id,
                run_id=previous.id,
                selected=True,
            )
        )
        session.commit()
        current_run_id = int(current.id)
        event_id = int(event.id)

    client = _EditorialClient()
    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=current_run_id,
        event_ids=[event_id],
        ai_client=client,
    )

    assert result.selected == 0
    assert client.calls[0][0]["recent_daily_history"] == {
        "appeared_recently": True,
        "prior_editions": ["2026-08-18"],
    }
    with session_factory() as session:
        current_snapshot = session.scalar(
            select(IntelEventStageDSnapshot).where(
                IntelEventStageDSnapshot.run_id == current_run_id,
                IntelEventStageDSnapshot.event_id == event_id,
            )
        )
        assert current_snapshot is not None
        assert current_snapshot.selected is False
        metadata = json.loads(current_snapshot.metadata_json)
        assert "recent_repeat_without_material_update" in metadata["reason_codes"]
