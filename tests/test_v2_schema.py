from __future__ import annotations

import sqlite3

from app.storage.db import create_engine_from_url, init_db


def test_fresh_database_contains_only_ai_core_tables(tmp_path):
    database = tmp_path / "fresh.db"
    engine = create_engine_from_url(f"sqlite:///{database}")
    init_db(engine)

    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "select name from sqlite_master "
                "where type = 'table' and name not like 'sqlite_%' order by name"
            )
        ]

    assert tables == [
        "ai_item_reviews",
        "fetch_attempts",
        "intel_event_items",
        "intel_event_ranking_snapshots",
        "intel_events",
        "intel_items",
        "intel_runs",
        "sources",
    ]


def test_init_db_adds_triage_columns_to_legacy_sqlite_without_data_loss(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE ai_item_reviews (
                id INTEGER PRIMARY KEY,
                item_id INTEGER NOT NULL,
                model VARCHAR(128),
                prompt_version VARCHAR(64) NOT NULL DEFAULT 'item_analysis_v1',
                keep BOOLEAN NOT NULL DEFAULT 0,
                content_class VARCHAR(64) NOT NULL DEFAULT 'community_social',
                summary_cn TEXT,
                reason TEXT,
                risk_flags_json TEXT NOT NULL DEFAULT '[]',
                confidence INTEGER NOT NULL DEFAULT 0,
                raw_response_json TEXT NOT NULL DEFAULT '{}',
                status VARCHAR(32) NOT NULL DEFAULT 'success',
                error_message TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO ai_item_reviews (id, item_id, content_class, summary_cn) VALUES (1, 7, 'project_tool', 'old row')"
        )
        connection.commit()

    engine = create_engine_from_url(f"sqlite:///{database}")
    init_db(engine)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(ai_item_reviews)")
        }
        row = connection.execute(
            "SELECT item_id, content_class, summary_cn, topics_json, keywords_json, scores_json, novelty_score, paper_support_json, topic_category "
            "FROM ai_item_reviews WHERE id = 1"
        ).fetchone()

    assert {
        "topic", "topics_json", "keywords_json", "selection_score", "scores_json",
        "novelty", "novelty_score", "paper_support_json", "topic_category",
    } <= columns
    assert row == (7, "project_tool", "old row", "[]", "[]", "{}", 0, "{}", None)
