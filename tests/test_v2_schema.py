from __future__ import annotations

import sqlite3

from app.storage.db import create_engine_from_url, init_db


def test_fresh_database_contains_v2_and_v3_tables(tmp_path):
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
        "cluster_decisions",
        "daily_editions",
        "daily_event_entries",
        "documents",
        "event_editorial_reviews",
        "event_evidence",
        "events",
        "fetch_attempts",
        "intel_items",
        "intel_runs",
        "item_verifications",
        "sources",
        "triage_reviews",
    ]
