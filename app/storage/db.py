from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import Base, DAILY_EDITION_TIMEZONE


# These tables are additive durable coordinator state.  ``init_db`` may create
# them in an otherwise complete Stage A/B database.  Apart from the narrowly
# scoped, fully-compatible ``intel_runs.edition_date`` upgrade below, startup
# must never alter existing tables or silently backfill legacy data.
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
