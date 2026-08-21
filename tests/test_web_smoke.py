from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import DailyEdition, DailyEditionReportEntry
from app.web.app import create_app


def _app(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'web.db'}")
    init_db(engine)
    sf = create_session_factory(engine)
    with sf() as session:
        edition = DailyEdition(
            edition_date=datetime(2026, 8, 16, tzinfo=timezone.utc).date(),
            status="published",
            published_at=datetime(2026, 8, 16, 6, 35, 31, tzinfo=timezone.utc),
        )
        session.add(edition)
        session.flush()
        session.add(
            DailyEditionReportEntry(
                edition_id=edition.id,
                event_key="url:https://web.example/update",
                display_order=1,
                title="Model update",
                original_title="<script>alert(1)</script> model update",
                summary="摘要",
                url="https://web.example/update",
                display_score=90,
                topic="model_release",
                content_class="official_model_company",
                source_group="official_blog",
                source_ids_json='["web_source"]',
                source_refs_json=json.dumps(
                    [
                        {
                            "source_id": "web_source",
                            "source_name": "Web source",
                            "source_group": "official_blog",
                            "source_url": "https://web.example/update",
                            "title": "<script>alert(1)</script> model update",
                            "match_type": "exact",
                            "is_primary": True,
                        }
                    ]
                ),
                verification_refs_json=json.dumps(
                    [
                        {
                            "url": "https://verify.example/proof",
                            "host": "verify.example",
                            "title": "Verification proof",
                            "status": "verified",
                            "claim": "确认发布动作",
                        }
                    ]
                ),
                metadata_json='{"reason_code":"material_change","reason":"变化明确"}',
            )
        )
        session.commit()
    return create_app(settings=Settings(database_url=f"sqlite:///{tmp_path / 'web.db'}"), session_factory=sf, init_database=False)


def test_home_reads_daily_edition_and_escapes_titles(tmp_path):
    response = TestClient(_app(tmp_path)).get("/?edition_date=2026-08-16")
    assert response.status_code == 200
    assert "Model update" in response.text
    assert 'href="/events/1?edition_date=2026-08-16&amp;origin=home"' in response.text
    assert "2026-08-16 日报精选" in response.text
    assert "EDITOR&#39;S PICK" not in response.text
    assert "今日头条" not in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_all_items_shows_only_selected_events_for_the_daily_edition(tmp_path):
    response = TestClient(_app(tmp_path)).get("/all?edition_date=2026-08-16")
    assert response.status_code == 200
    assert "Model update" in response.text
    assert "Excluded Stage A item" not in response.text
    assert "Stage A 决策" not in response.text


def test_event_detail_keeps_excluded_members_internal(tmp_path):
    response = TestClient(_app(tmp_path)).get("/events/1?edition_date=2026-08-16&origin=home")
    assert response.status_code == 200
    assert "关联资讯与筛选追溯" in response.text
    assert "独立核验来源" in response.text
    assert "Verification proof" in response.text
    assert "Model update" in response.text
    assert "Excluded Stage A item" not in response.text
    assert 'nav-item active" href="/?edition_date=2026-08-16"' in response.text
    assert 'nav-item active" href="/all?edition_date=2026-08-16"' not in response.text
    assert 'href="/?edition_date=2026-08-16">返回今日精选</a>' in response.text


def test_daily_edition_api_returns_selected_events_only(tmp_path):
    client = TestClient(_app(tmp_path))
    current = client.get("/api/ui/current?edition_date=2026-08-16")
    assert current.status_code == 200
    current_payload = current.json()
    assert current_payload["edition"]["edition_date"] == "2026-08-16"
    assert current_payload["count"] == 1
    assert current_payload["events"][0]["title"] == "Model update"
    assert "Excluded Stage A item" not in json.dumps(current_payload, ensure_ascii=False)
    assert "run_id" not in json.dumps(current_payload, ensure_ascii=False)
    assert "snapshot_key" not in json.dumps(current_payload, ensure_ascii=False)

    events = client.get("/api/editions/2026-08-16/events")
    assert events.status_code == 200
    assert events.json()["funnel"]["selected"] == 1

    detail = client.get("/api/editions/2026-08-16/events/1")
    assert detail.status_code == 200
    assert detail.json()["event"]["members"][0]["title"] == "<script>alert(1)</script> model update"
    assert detail.json()["event"]["event"]["verification_refs"][0]["status"] == "verified"
    assert "raw_payload" not in json.dumps(detail.json(), ensure_ascii=False)
    assert "run_id" not in json.dumps(detail.json(), ensure_ascii=False)
