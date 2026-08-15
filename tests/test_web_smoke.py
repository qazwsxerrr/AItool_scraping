from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, AIItemScreen, IntelEvent, IntelEventItem, IntelEventRankingSnapshot, IntelItem, Source
from app.web.app import create_app


def _app(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'web.db'}")
    init_db(engine)
    sf = create_session_factory(engine)
    with sf() as session:
        source = Source(id="web_source", name="Web source", transport="feed", url="https://web.example", source_group="official_blog", content_class="official_model_company")
        item = IntelItem(source=source, title="<script>alert(1)</script> model update", canonical_url="https://web.example/update", content_class="official_model_company", content_hash="w" * 64, status="candidate", selection_score=90, captured_at=datetime.now(timezone.utc))
        session.add_all([source, item])
        session.flush()
        session.add_all([
            AIItemScreen(item=item, decision="pass", reason_code="signal", reason="useful", confidence=94),
            AIItemReview(item=item, content_class="official_model_company", topic="model", topics_json='["model"]', summary_cn="摘要", selection_score=90, status="success"),
        ])
        session.flush()
        event = IntelEvent(event_key="title:model update", title="Model update", summary_cn="摘要", topic="model", content_class="official_model_company", display_score=90, new_in_run_id=1, first_seen_at=datetime.now(timezone.utc))
        session.add(event)
        session.flush()
        session.add(IntelEventItem(event=event, item=item, source=source, source_id=source.id, is_primary=True, match_type="exact"))
        session.add(IntelEventRankingSnapshot(snapshot_key="latest", event_id=event.id, run_id=1, rank=1, display_score=90, selected=True, topic="model", content_class="official_model_company"))
        session.commit()
    return create_app(settings=Settings(database_url=f"sqlite:///{tmp_path / 'web.db'}"), session_factory=sf, init_database=False)


def test_home_reads_snapshot_and_escapes_titles(tmp_path):
    response = TestClient(_app(tmp_path)).get("/")
    assert response.status_code == 200
    assert "Model update" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_all_items_exposes_stage_a_filter_without_job_execution(tmp_path):
    response = TestClient(_app(tmp_path)).get("/all?screen_decision=pass")
    assert response.status_code == 200
    assert "Stage A pass" in response.text
