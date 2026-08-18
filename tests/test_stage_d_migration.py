from __future__ import annotations

import json

from sqlalchemy import inspect, text

from app.storage.db import create_engine_from_url, migrate_rank_to_stage_d


def test_explicit_rank_to_stage_d_migration_preserves_snapshots_and_audit(tmp_path):
    database = tmp_path / "legacy-rank.db"
    engine = create_engine_from_url(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE intel_events (id INTEGER PRIMARY KEY, identity_keys_json TEXT NOT NULL)"))
        connection.execute(text("CREATE TABLE intel_runs (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE intel_run_stages ("
                "id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, stage_name TEXT NOT NULL, "
                "config_fingerprint TEXT, result_ref_json TEXT NOT NULL, metadata_json TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE intel_run_stage_tasks ("
                "id INTEGER PRIMARY KEY, stage_id INTEGER NOT NULL, config_fingerprint TEXT, "
                "result_ref_json TEXT NOT NULL, result_json TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE intel_run_stage_attempts ("
                "id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, config_fingerprint TEXT, "
                "result_ref_json TEXT NOT NULL, metadata_json TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE intel_event_ranking_snapshots ("
                "id INTEGER PRIMARY KEY, snapshot_key TEXT NOT NULL, event_id INTEGER NOT NULL, run_id INTEGER, "
                "rank INTEGER NOT NULL, display_score FLOAT NOT NULL, selected BOOLEAN NOT NULL, "
                "topic TEXT, source_group TEXT, content_class TEXT, reason TEXT, metadata_json TEXT NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO intel_events (id, identity_keys_json) VALUES (1, :aliases)"), {"aliases": json.dumps(["url:https://example.test/a", "title:old-title"])})
        connection.execute(text("INSERT INTO intel_runs (id) VALUES (1)"))
        connection.execute(
            text(
                "INSERT INTO intel_run_stages "
                "(id, run_id, stage_name, config_fingerprint, result_ref_json, metadata_json) "
                "VALUES (1, 1, 'rank', 'rank-v2', :result_ref, :metadata)"
            ),
            {
                "result_ref": json.dumps({"projection": "IntelEventRankingSnapshot"}),
                "metadata": json.dumps({"rank_source": "deterministic"}),
            },
        )
        connection.execute(
            text(
                "INSERT INTO intel_run_stage_tasks "
                "(id, stage_id, config_fingerprint, result_ref_json, result_json) "
                "VALUES (1, 1, 'rank-v2', :result_ref, :result)"
            ),
            {
                "result_ref": json.dumps({"projection": "IntelEventRankingSnapshot"}),
                "result": json.dumps({"rank_total": 1, "rank_selected": 1, "rank_source": "deterministic"}),
            },
        )
        connection.execute(
            text(
                "INSERT INTO intel_run_stage_attempts "
                "(id, task_id, config_fingerprint, result_ref_json, metadata_json) "
                "VALUES (1, 1, 'rank-v2', :result_ref, :metadata)"
            ),
            {
                "result_ref": json.dumps({"projection": "IntelEventRankingSnapshot"}),
                "metadata": json.dumps({"rank_source": "deterministic"}),
            },
        )
        connection.execute(
            text(
                "INSERT INTO intel_event_ranking_snapshots "
                "(id, snapshot_key, event_id, run_id, rank, display_score, selected, topic, source_group, content_class, reason, metadata_json, created_at, updated_at) "
                "VALUES (1, 'daily-2026-08-17', 1, 1, 3, 88, 1, 'model', 'official_blog', 'official_model_company', 'selected', :metadata, :created_at, :updated_at)"
            ),
            {
                "metadata": json.dumps({"rank_source": "deterministic"}),
                "created_at": "2026-08-17 00:00:00",
                "updated_at": "2026-08-17 00:00:00",
            },
        )

    dry_run = migrate_rank_to_stage_d(engine)
    assert dry_run["applied"] is False
    assert "rebuild intel_event_ranking_snapshots as intel_event_stage_d_snapshots and rename rank to display_order" in dry_run["actions"]

    result = migrate_rank_to_stage_d(engine, apply=True)
    assert result["migrated"] is True
    inspector = inspect(engine)
    assert "intel_event_ranking_snapshots" not in inspector.get_table_names()
    assert "intel_event_stage_d_snapshots" in inspector.get_table_names()
    assert {row["name"] for row in inspector.get_indexes("intel_event_stage_d_snapshots")} >= {
        "ix_intel_event_stage_d_snapshot_key",
        "ix_intel_event_stage_d_snapshot_order",
        "ix_intel_event_stage_d_snapshot_selected",
    }

    with engine.connect() as connection:
        snapshot = connection.execute(
            text("SELECT display_order, metadata_json FROM intel_event_stage_d_snapshots WHERE id = 1")
        ).mappings().one()
        stage = connection.execute(
            text("SELECT stage_name, config_fingerprint, result_ref_json, metadata_json FROM intel_run_stages WHERE id = 1")
        ).mappings().one()
        task = connection.execute(
            text("SELECT config_fingerprint, result_ref_json, result_json FROM intel_run_stage_tasks WHERE id = 1")
        ).mappings().one()
        attempt = connection.execute(
            text("SELECT config_fingerprint, result_ref_json, metadata_json FROM intel_run_stage_attempts WHERE id = 1")
        ).mappings().one()
        aliases = connection.execute(text("SELECT identity_keys_json FROM intel_events WHERE id = 1")).scalar_one()

    assert snapshot["display_order"] == 3
    assert json.loads(snapshot["metadata_json"]) == {"stage": "stage_d", "stage_d_source": "deterministic"}
    assert stage["stage_name"] == "stage_d"
    assert "rank" not in stage["config_fingerprint"]
    assert json.loads(stage["result_ref_json"])["projection"] == "IntelEventStageDSnapshot"
    assert json.loads(stage["metadata_json"])["stage_d_source"] == "deterministic"
    assert "rank" not in task["config_fingerprint"]
    assert json.loads(task["result_json"])["stage_d_total"] == 1
    assert json.loads(task["result_json"])["stage_d_selected"] == 1
    assert json.loads(attempt["metadata_json"])["stage_d_source"] == "deterministic"
    assert json.loads(aliases) == ["url:https://example.test/a"]
