from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, bindparam, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import Base, DAILY_EDITION_TIMEZONE


# These tables are additive durable coordinator state.  ``init_db`` may create
# them in an otherwise complete Stage A/B database.  Apart from the narrowly
# scoped, fully-compatible ``intel_runs.edition_date`` upgrade below, startup
# must never alter existing tables or silently backfill legacy data.  The
# Rank-to-Stage-D conversion is deliberately exposed as an explicit script.
STATE_TABLE_NAMES = frozenset(
    {
        "intel_run_stages",
        "intel_run_stage_tasks",
        "intel_run_stage_attempts",
    }
)


def create_engine_from_url(database_url: str, *, echo: bool = False) -> Engine:
    url = make_url(database_url)
    connect_args = {}
    if url.drivername.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        _ensure_sqlite_parent(database_url)
    return create_engine(database_url, echo=echo, future=True, connect_args=connect_args)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False, future=True)


def init_db(engine: Engine) -> None:
    _upgrade_intel_run_edition_date(engine)
    _assert_fresh_or_compatible_schema(engine)
    Base.metadata.create_all(bind=engine)
    _ensure_intel_run_edition_date_index(engine)


def migrate_rank_to_stage_d(engine: Engine, *, apply: bool = False) -> dict[str, object]:
    """Explicitly migrate the historical Rank storage contract to Stage D.

    Startup intentionally never invokes this routine: a database migration is
    an operator action.  The supported project database is SQLite, where this
    rebuild preserves every historical snapshot row while changing the table
    and column names atomically inside one transaction.  The function returns
    a dry-run/apply report that the CLI script can print or persist in logs.
    """

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    old_table = "intel_event_ranking_snapshots"
    new_table = "intel_event_stage_d_snapshots"
    actions: list[str] = []
    if old_table in tables and new_table in tables:
        raise RuntimeError("both Rank and Stage D snapshot tables exist; resolve the migration state explicitly")
    if old_table in tables:
        actions.append(f"rebuild {old_table} as {new_table} and rename rank to display_order")
    if "intel_run_stages" in tables:
        actions.append("rename durable stage rows from rank to stage_d")
    if not actions:
        return {"applied": bool(apply), "migrated": False, "actions": []}
    if not apply:
        return {"applied": False, "migrated": False, "actions": actions}
    if engine.dialect.name != "sqlite":
        raise RuntimeError("Rank-to-Stage-D migration currently supports SQLite only; export/back up and migrate this database explicitly")

    with engine.begin() as connection:
        rank_stage_ids = _rank_stage_ids(connection, tables)
        _assert_no_stage_d_collision(connection, tables)
        if old_table in tables:
            target = Base.metadata.tables[new_table]
            target.create(bind=connection, checkfirst=False)
            connection.execute(
                text(
                    "INSERT INTO intel_event_stage_d_snapshots "
                    "(id, snapshot_key, event_id, run_id, display_order, display_score, selected, topic, source_group, content_class, reason, metadata_json, created_at, updated_at) "
                    "SELECT id, snapshot_key, event_id, run_id, rank, display_score, selected, topic, source_group, content_class, reason, metadata_json, created_at, updated_at "
                    "FROM intel_event_ranking_snapshots"
                )
            )
            connection.execute(text("DROP TABLE intel_event_ranking_snapshots"))
        _migrate_snapshot_metadata(connection, new_table)
        _migrate_rank_stage_audit(connection, tables, rank_stage_ids)
        _remove_legacy_title_identity_aliases(connection, tables)
    return {"applied": True, "migrated": True, "actions": actions}


def _table_columns(connection, table_name: str) -> set[str]:
    return {row["name"] for row in inspect(connection).get_columns(table_name)}


def _rank_stage_ids(connection, tables: set[str]) -> list[int]:
    if "intel_run_stages" not in tables:
        return []
    columns = _table_columns(connection, "intel_run_stages")
    if not {"id", "stage_name"}.issubset(columns):
        return []
    return [
        int(value)
        for value in connection.scalars(
            text("SELECT id FROM intel_run_stages WHERE stage_name = 'rank'")
        ).all()
    ]


def _assert_no_stage_d_collision(connection, tables: set[str]) -> None:
    """Refuse ambiguous stage conversion rather than silently dropping audit."""

    if "intel_run_stages" not in tables:
        return
    columns = _table_columns(connection, "intel_run_stages")
    if not {"run_id", "stage_name"}.issubset(columns):
        return
    collisions = connection.execute(
        text(
            "SELECT legacy.run_id FROM intel_run_stages AS legacy "
            "JOIN intel_run_stages AS current ON current.run_id = legacy.run_id "
            "WHERE legacy.stage_name = 'rank' AND current.stage_name = 'stage_d' "
            "LIMIT 1"
        )
    ).first()
    if collisions is not None:
        raise RuntimeError(
            "both rank and stage_d rows exist for one run; resolve the durable stage collision explicitly"
        )


def _migrate_snapshot_metadata(connection, table_name: str) -> None:
    if table_name not in set(inspect(connection).get_table_names()):
        return
    columns = _table_columns(connection, table_name)
    if not {"id", "metadata_json"}.issubset(columns):
        return
    rows = connection.execute(
        text(f"SELECT id, metadata_json FROM {table_name}")
    ).mappings().all()
    updates: list[dict[str, object]] = []
    for row in rows:
        value, changed = _rewrite_rank_json(row.get("metadata_json"), force_stage=True)
        if changed:
            updates.append({"id": int(row["id"]), "metadata_json": value})
    if updates:
        connection.execute(
            text(f"UPDATE {table_name} SET metadata_json = :metadata_json WHERE id = :id"),
            updates,
        )


def _migrate_rank_stage_audit(connection, tables: set[str], rank_stage_ids: list[int]) -> None:
    """Rename stage rows and every mutable rank-specific audit envelope."""

    if "intel_run_stages" not in tables:
        return
    stage_columns = _table_columns(connection, "intel_run_stages")
    if "stage_name" not in stage_columns:
        return
    if rank_stage_ids:
        if "metadata_json" in stage_columns:
            _rewrite_json_column(connection, "intel_run_stages", "metadata_json", "id", rank_stage_ids, force_stage=True)
        if "result_ref_json" in stage_columns:
            _rewrite_json_column(connection, "intel_run_stages", "result_ref_json", "id", rank_stage_ids, force_stage=True)
        if "config_fingerprint" in stage_columns:
            connection.execute(
                _ids_statement(
                    "UPDATE intel_run_stages "
                    "SET config_fingerprint = REPLACE(config_fingerprint, 'rank', 'stage_d') "
                    "WHERE id IN :ids AND config_fingerprint IS NOT NULL"
                ),
                {"ids": rank_stage_ids},
            )
    connection.execute(text("UPDATE intel_run_stages SET stage_name = 'stage_d' WHERE stage_name = 'rank'"))

    if not rank_stage_ids or "intel_run_stage_tasks" not in tables:
        return
    task_columns = _table_columns(connection, "intel_run_stage_tasks")
    task_ids = [
        int(value)
        for value in connection.scalars(
            _ids_statement("SELECT id FROM intel_run_stage_tasks WHERE stage_id IN :ids"),
            {"ids": rank_stage_ids},
        ).all()
    ]
    for column in ("result_ref_json", "result_json"):
        if column in task_columns:
            _rewrite_json_column(connection, "intel_run_stage_tasks", column, "id", task_ids, force_stage=True)
    if "config_fingerprint" in task_columns and task_ids:
        connection.execute(
            _ids_statement(
                "UPDATE intel_run_stage_tasks "
                "SET config_fingerprint = REPLACE(config_fingerprint, 'rank', 'stage_d') "
                "WHERE id IN :ids AND config_fingerprint IS NOT NULL"
            ),
            {"ids": task_ids},
        )

    if not task_ids or "intel_run_stage_attempts" not in tables:
        return
    attempt_columns = _table_columns(connection, "intel_run_stage_attempts")
    for column in ("result_ref_json", "metadata_json"):
        if column in attempt_columns:
            _rewrite_json_column(connection, "intel_run_stage_attempts", column, "task_id", task_ids, force_stage=True)
    if "config_fingerprint" in attempt_columns:
        connection.execute(
            _ids_statement(
                "UPDATE intel_run_stage_attempts "
                "SET config_fingerprint = REPLACE(config_fingerprint, 'rank', 'stage_d') "
                "WHERE task_id IN :ids AND config_fingerprint IS NOT NULL"
            ),
            {"ids": task_ids},
        )


def _rewrite_json_column(
    connection,
    table_name: str,
    column_name: str,
    selector_column: str,
    ids: list[int],
    *,
    force_stage: bool,
) -> None:
    if not ids:
        return
    rows = connection.execute(
        _ids_statement(
            f"SELECT id, {column_name} FROM {table_name} "
            f"WHERE {selector_column} IN :ids"
        ),
        {"ids": ids},
    ).mappings().all()
    updates: list[dict[str, object]] = []
    for row in rows:
        value, changed = _rewrite_rank_json(row.get(column_name), force_stage=force_stage)
        if changed:
            updates.append({"id": int(row["id"]), column_name: value})
    if updates:
        connection.execute(
            text(f"UPDATE {table_name} SET {column_name} = :{column_name} WHERE id = :id"),
            updates,
        )


def _rewrite_rank_json(value: object, *, force_stage: bool) -> tuple[str, bool]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}
    rewritten, changed = _rewrite_rank_value(parsed)
    if not isinstance(rewritten, dict):
        rewritten = {}
        changed = True
    if force_stage and rewritten.get("stage") != "stage_d":
        rewritten["stage"] = "stage_d"
        changed = True
    return json.dumps(rewritten, ensure_ascii=False, sort_keys=True), changed


def _ids_statement(sql: str):
    return text(sql).bindparams(bindparam("ids", expanding=True))


def _rewrite_rank_value(value: object) -> tuple[object, bool]:
    if isinstance(value, list):
        rows: list[object] = []
        changed = False
        for item in value:
            rewritten, item_changed = _rewrite_rank_value(item)
            rows.append(rewritten)
            changed = changed or item_changed
        return rows, changed
    if not isinstance(value, dict):
        return value, False
    renamed = {
        "rank_source": "stage_d_source",
        "rank_total": "stage_d_total",
        "rank_selected": "stage_d_selected",
        "ranked_count": "stage_d_count",
    }
    result: dict[str, object] = {}
    changed = False
    for raw_key, raw_value in value.items():
        key = renamed.get(str(raw_key), str(raw_key))
        rewritten, item_changed = _rewrite_rank_value(raw_value)
        if key == "stage" and rewritten in {"rank", "editorial_rank"}:
            rewritten = "stage_d"
            item_changed = True
        if key == "projection" and rewritten == "IntelEventRankingSnapshot":
            rewritten = "IntelEventStageDSnapshot"
            item_changed = True
        result[key] = rewritten
        changed = changed or item_changed or key != raw_key
    return result, changed


def _remove_legacy_title_identity_aliases(connection, tables: set[str]) -> None:
    """Stop pre-v2 title aliases from being used as durable exact identity."""

    if "intel_events" not in tables:
        return
    columns = _table_columns(connection, "intel_events")
    if not {"id", "identity_keys_json"}.issubset(columns):
        return
    rows = connection.execute(text("SELECT id, identity_keys_json FROM intel_events")).mappings().all()
    updates: list[dict[str, object]] = []
    for row in rows:
        try:
            aliases = json.loads(str(row.get("identity_keys_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            aliases = []
        if not isinstance(aliases, list):
            aliases = []
        filtered = [alias for alias in aliases if not (isinstance(alias, str) and alias.startswith("title:"))]
        if filtered != aliases:
            updates.append({"id": int(row["id"]), "identity_keys_json": json.dumps(filtered, ensure_ascii=False)})
    if updates:
        connection.execute(text("UPDATE intel_events SET identity_keys_json = :identity_keys_json WHERE id = :id"), updates)


def _upgrade_intel_run_edition_date(engine: Engine) -> None:
    """Add and backfill the indexed daily label on an otherwise complete DB.

    The project normally rejects arbitrary legacy schemas rather than trying
    to repair them.  This is intentionally narrower: only a complete current
    schema missing *exactly* the additive ``intel_runs.edition_date`` column
    is upgraded.  That keeps existing local databases usable without changing
    run IDs, foreign keys, or historical audit rows.
    """

    inspector = inspect(engine)
    if "intel_runs" not in inspector.get_table_names():
        return
    actual = {column["name"] for column in inspector.get_columns("intel_runs")}
    expected = set(Base.metadata.tables["intel_runs"].columns.keys())
    missing = expected - actual
    if missing != {"edition_date"}:
        return

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE intel_runs ADD COLUMN edition_date DATE"))
        rows = connection.execute(
            text(
                "SELECT id, scope_json, started_at "
                "FROM intel_runs WHERE edition_date IS NULL"
            )
        ).mappings()
        updates = [
            {"id": int(row["id"]), "edition_date": value}
            for row in rows
            if (value := _legacy_edition_date(row.get("scope_json"), row.get("started_at"))) is not None
        ]
        if updates:
            connection.execute(
                text("UPDATE intel_runs SET edition_date = :edition_date WHERE id = :id"),
                updates,
            )


def _ensure_intel_run_edition_date_index(engine: Engine) -> None:
    """Create the new date index for both fresh and additive-upgrade DBs."""

    inspector = inspect(engine)
    if "intel_runs" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("intel_runs")}
    if "edition_date" not in columns:
        return
    table = Base.metadata.tables["intel_runs"]
    index = next((value for value in table.indexes if value.name == "ix_intel_runs_edition_date"), None)
    if index is None:
        return
    with engine.begin() as connection:
        index.create(bind=connection, checkfirst=True)


def _legacy_edition_date(scope_json: object, started_at: object) -> str | None:
    """Derive a Shanghai daily label for a pre-column run row."""

    scope: object = {}
    try:
        scope = json.loads(str(scope_json or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        scope = {}
    if isinstance(scope, dict):
        value = scope.get("edition_date")
        if value:
            try:
                return date.fromisoformat(str(value)).isoformat()
            except (TypeError, ValueError):
                pass
        reference = scope.get("reference_time")
    else:
        reference = None
    current = _as_utc_datetime(reference) or _as_utc_datetime(started_at)
    return current.astimezone(DAILY_EDITION_TIMEZONE).date().isoformat() if current is not None else None


def _as_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _assert_fresh_or_compatible_schema(engine: Engine) -> None:
    """Reject pre-Stage-A databases instead of mutating them in place.

    ``MetaData.create_all`` is intentionally create-only.  Existing local
    SQLite files from the old single-call triage pipeline are not migrated or
    backfilled; callers must explicitly remove/reinitialize those files.  A
    complete schema created by this metadata is accepted for idempotent
    startup.  The only migration exception is the one-column, additive run
    edition upgrade performed before this compatibility check.
    """

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    if not existing:
        return

    # Ignore SQLite's internal bookkeeping tables, but reject any user table
    # set that is not the fresh contract.  This catches both old schemas with
    # missing Stage A/B columns and partial databases that would otherwise
    # fail later on the first query.
    user_tables = {name for name in existing if not name.startswith("sqlite_")}
    # Existing databases from before resumable stages are accepted when their
    # complete legacy contract is present.  Missing coordinator tables are
    # additive and will be created by ``create_all`` below.
    if "intel_event_ranking_snapshots" in user_tables:
        raise RuntimeError(
            "Existing database contains the retired Rank snapshot schema. "
            "Run scripts/migrate_rank_to_stage_d.py --apply explicitly before starting the Stage D pipeline."
        )
    missing_tables = (expected - STATE_TABLE_NAMES) - user_tables
    extra_tables = user_tables - expected
    incompatible_columns: dict[str, set[str]] = {}
    for table_name in expected & user_tables:
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = set(Base.metadata.tables[table_name].columns.keys())
        missing_columns = expected_columns - actual_columns
        if missing_columns:
            incompatible_columns[table_name] = missing_columns

    if missing_tables or extra_tables or incompatible_columns:
        details: list[str] = []
        if missing_tables:
            details.append("missing tables=" + ",".join(sorted(missing_tables)))
        if extra_tables:
            details.append("unexpected tables=" + ",".join(sorted(extra_tables)))
        if incompatible_columns:
            details.extend(
                f"{table} missing columns={','.join(sorted(columns))}"
                for table, columns in sorted(incompatible_columns.items())
            )
        raise RuntimeError(
            "Existing database schema is incompatible with the fresh Stage A/B "
            "intelligence schema (no migrations/backfill are supported): "
            + "; ".join(details)
            + ". Reinitialize the database explicitly."
        )


def _ensure_sqlite_parent(database_url: str) -> None:
    if database_url in {"sqlite://", "sqlite:///:memory:"}:
        return
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return
    path_text = database_url[len(prefix) :]
    if not path_text or path_text == ":memory:":
        return
    path = Path(path_text)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
