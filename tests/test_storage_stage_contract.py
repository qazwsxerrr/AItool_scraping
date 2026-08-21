from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, AIItemScreen, IntelItem, IntelRun, IntelRunItem, Source
from app.storage.repository import IntelRepository


def _factory(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'intel.db'}")
    init_db(engine)
    return create_session_factory(engine), engine


def test_fresh_schema_contains_stage_rows_and_run_scope(tmp_path):
    _factory(tmp_path)
    with sqlite3.connect(tmp_path / "intel.db") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        }
    assert {"ai_item_screens", "ai_item_reviews", "intel_runs", "intel_run_items"} <= tables


def test_fresh_schema_uses_only_canonical_b1_priority_columns(tmp_path):
    _factory(tmp_path)
    with sqlite3.connect(tmp_path / "intel.db") as connection:
        for table in ("intel_items", "ai_item_reviews"):
            columns = {row[1] for row in connection.execute(f"pragma table_info({table})")}
            assert "b1_priority" in columns
            assert "selection_score" not in columns


def test_old_schema_fails_fast_without_alter_or_backfill(tmp_path):
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as connection:
        connection.execute("create table ai_item_reviews (id integer primary key, item_id integer not null)")
        connection.commit()
    engine = create_engine_from_url(f"sqlite:///{database}")
    with pytest.raises(RuntimeError, match="unsupported.*no conversion"):
        init_db(engine)


def test_stage_a_b_raw_payloads_and_run_scope_are_serially_persisted(tmp_path):
    session_factory, _ = _factory(tmp_path)
    with session_factory() as session:
        session.add(
            Source(
                id="source-1",
                name="Source",
                transport="rss",
                url="https://example.test/feed",
                content_class="community_social",
            )
        )
        session.commit()
        repository = IntelRepository(session)
        _, run = repository.start_daily_build(
            edition_date="2026-08-19",
            scope={"source_limit": 30},
            source_ids=["source-1"],
        )
        inserted = repository.insert_item(
            {
                "source_id": "source-1",
                "external_id": "item-1",
                "title": "A useful release",
                "content_class": "community_social",
                "content_hash": "hash-1",
            },
            run_id=run.id,
        )
        assert inserted.item_id is not None
        screen = repository.upsert_ai_screen(
            inserted.item_id,
            {
                "decision": "uncertain",
                "reason_code": "needs_analysis",
                "reason": "ambiguous",
                "confidence": 62,
                "risk_flags": ["source:social_only"],
                "raw_response": {"provider": "screen"},
            },
            run_id=run.id,
        )
        analysis = repository.upsert_ai_analysis(
            inserted.item_id,
            {
                "topic": "product_application",
                "topics": ["product_application"],
                "summary_cn": "一段摘要",
                "keywords": ["release"],
                "entities": [{"name": "Acme", "type": "company"}],
                "b1_priority": 88,
                "score_components": {
                    "audience_relevance": 88,
                    "material_change": 88,
                    "impact_scope": 80,
                    "independent_news_value": 88,
                    "specificity": 88,
                },
                "raw_response": {"provider": "analysis"},
            },
            run_id=run.id,
            content_class="community_social",
        )
        repository.set_item_status(inserted.item_id, "candidate", run_id=run.id)
        session.commit()

        assert session.scalar(select(IntelRunItem).where(IntelRunItem.run_id == run.id)).item_id == inserted.item_id
        assert session.scalar(select(AIItemScreen).where(AIItemScreen.item_id == inserted.item_id)).raw_response == {
            "provider": "screen"
        }
        persisted = session.scalar(select(AIItemReview).where(AIItemReview.item_id == inserted.item_id))
        assert persisted is not None
        assert persisted.entities == [{"name": "Acme", "type": "company"}]
        assert persisted.raw_response == {"provider": "analysis"}
        assert session.get(IntelItem, inserted.item_id).status == "candidate"
        assert session.get(IntelRun, run.id).item_ids_json == "[1]"
