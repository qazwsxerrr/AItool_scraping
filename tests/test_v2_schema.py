from __future__ import annotations

import sqlite3

from app.storage.db import create_engine_from_url, init_db


def test_fresh_database_contains_only_v2_tables(tmp_path):
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
        "intel_items",
        "intel_runs",
        "item_verifications",
        "sources",
    ]
