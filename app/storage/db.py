from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import Base


# These tables are additive durable coordinator state.  ``init_db`` may create
# them in an otherwise complete Stage A/B database, but it must never attempt
# to alter existing tables or silently backfill legacy data.
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
    _assert_fresh_or_compatible_schema(engine)
    Base.metadata.create_all(bind=engine)


def _assert_fresh_or_compatible_schema(engine: Engine) -> None:
    """Reject pre-Stage-A databases instead of mutating them in place.

    ``MetaData.create_all`` is intentionally create-only.  Existing local
    SQLite files from the old single-call triage pipeline are not migrated or
    backfilled; callers must explicitly remove/reinitialize those files.  A
    complete schema created by this metadata is accepted for idempotent
    startup.
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
