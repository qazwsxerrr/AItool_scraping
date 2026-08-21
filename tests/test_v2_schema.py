from __future__ import annotations

import sqlite3

import pytest

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
        "ai_item_screens",
        "daily_edition_report_entries",
        "daily_editions",
        "fetch_attempts",
        "intel_event_items",
        "intel_events",
        "intel_items",
        "intel_run_items",
        "intel_run_stage_attempts",
        "intel_run_stage_tasks",
        "intel_run_stages",
        "intel_runs",
        "sources",
    ]


def test_init_db_rejects_incompatible_legacy_sqlite_without_backfill(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ai_item_reviews (id INTEGER PRIMARY KEY)")
        connection.commit()

    engine = create_engine_from_url(f"sqlite:///{database}")
    with pytest.raises(RuntimeError, match="unsupported.*no conversion"):
        init_db(engine)
