from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, AIItemScreen, IntelEvent, IntelEventItem, IntelEventStageDSnapshot, IntelItem, Source
from app.storage.repository import IntelRepository
from app.web.app import create_app
from app.web.routes.api import _strip_internal_run_fields


def _app(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'web.db'}")
    init_db(engine)
    sf = create_session_factory(engine)
    with sf() as session:
        run = IntelRepository(session).start_run(
            reference_time=datetime(2026, 8, 16, 6, 35, 31, tzinfo=timezone.utc),
            run_type="pipeline",
        )
        source = Source(id="web_source", name="Web source", transport="feed", url="https://web.example", source_group="official_blog", content_class="official_model_company")
        item = IntelItem(source=source, title="<script>alert(1)</script> model update", canonical_url="https://web.example/update", content_class="official_model_company", content_hash="w" * 64, status="candidate", selection_score=90, captured_at=datetime.now(timezone.utc))
        session.add_all([source, item])
        session.flush()
        session.add_all([
            AIItemScreen(item=item, decision="pass", reason_code="signal", reason="useful", confidence=94),
            AIItemReview(item=item, content_class="official_model_company", topic="model", topics_json='["model"]', summary_cn="摘要", selection_score=90, status="success"),
        ])
        session.flush()
        event = IntelEvent(event_key="title:model update", title="Model update", summary_cn="摘要", topic="model", content_class="official_model_company", display_score=90, new_in_run_id=run.id, first_seen_at=datetime.now(timezone.utc))
        session.add(event)
        session.flush()
        session.add(IntelEventItem(event=event, item=item, source=source, source_id=source.id, is_primary=True, match_type="exact"))
        session.add(IntelEventStageDSnapshot(snapshot_key="daily-2026-08-16", event_id=event.id, run_id=run.id, display_order=1, display_score=90, selected=True, topic="model", content_class="official_model_company", metadata_json='{"display_title_zh":"Model update","title_supporting_fields":["title"]}'))
        excluded = IntelItem(
            source=source,
            title="Excluded Stage A item",
            content_class="official_model_company",
            content_hash="x" * 64,
            status="screened_out",
        )
        session.add(excluded)
        session.flush()
        session.add(
            IntelEventItem(
                event=event,
                item=excluded,
                source=source,
                source_id=source.id,
                is_primary=False,
                match_type="related",
            )
        )
        session.commit()
    return create_app(settings=Settings(database_url=f"sqlite:///{tmp_path / 'web.db'}"), session_factory=sf, init_database=False)


def test_home_reads_snapshot_and_escapes_titles(tmp_path):
    response = TestClient(_app(tmp_path)).get("/?run_date=2026-08-16")
    assert response.status_code == 200
    assert "Model update" in response.text
    assert 'href="/events/1?run_date=2026-08-16&amp;origin=home"' in response.text
    assert "2026-08-16 日报精选" in response.text
    assert "EDITOR&#39;S PICK" not in response.text
    assert "今日头条" not in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_all_items_shows_only_selected_events_for_the_daily_edition(tmp_path):
    response = TestClient(_app(tmp_path)).get("/all?run_date=2026-08-16")
    assert response.status_code == 200
    assert "Model update" in response.text
    assert "Excluded Stage A item" not in response.text
    assert "Stage A 决策" not in response.text


def test_event_detail_keeps_excluded_members_internal(tmp_path):
    response = TestClient(_app(tmp_path)).get("/events/1?run_date=2026-08-16&origin=home")
    assert response.status_code == 200
    assert "关联资讯与筛选追溯" in response.text
    assert "Model update" in response.text
    assert "Excluded Stage A item" not in response.text
    assert 'nav-item active" href="/?run_date=2026-08-16"' in response.text
    assert 'nav-item active" href="/all?run_date=2026-08-16"' not in response.text
    assert 'href="/?run_date=2026-08-16">返回今日精选</a>' in response.text


def test_run_snapshot_api_returns_selected_events_only(tmp_path):
    client = TestClient(_app(tmp_path))
    current = client.get("/api/ui/current?run_date=2026-08-16")
    assert current.status_code == 200
    current_payload = current.json()
    assert current_payload["snapshot"]["edition_date"] == "2026-08-16"
    assert current_payload["count"] == 1
    assert current_payload["events"][0]["title"] == "Model update"
    assert "Excluded Stage A item" not in json.dumps(current_payload, ensure_ascii=False)
    assert "run_id" not in json.dumps(current_payload, ensure_ascii=False)
    assert "snapshot_key" not in json.dumps(current_payload, ensure_ascii=False)

    events = client.get("/api/snapshots/daily-2026-08-16/events")
    assert events.status_code == 200
    assert events.json()["funnel"]["stage_d_selected"] == 1
    assert client.get("/api/snapshots/daily-2026-08-16/events?run_id=99").status_code == 200

    detail = client.get("/api/snapshots/daily-2026-08-16/events/1")
    assert detail.status_code == 200
    assert detail.json()["event"]["members"][0]["title"] == "<script>alert(1)</script> model update"
    assert "raw_payload" not in json.dumps(detail.json(), ensure_ascii=False)
    assert "run_id" not in json.dumps(detail.json(), ensure_ascii=False)


def test_public_api_strips_nested_internal_execution_identifiers():
    payload = {
        "run_id": 9,
        "safe": {
            "daily_repeat_prior_run_id": 8,
            "daily_repeat_prior_snapshot_key": "run-8",
            "title": "visible",
        },
    }

    assert _strip_internal_run_fields(payload) == {"safe": {"title": "visible"}}
