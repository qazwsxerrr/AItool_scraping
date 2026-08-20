from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import Base


# Kept as a schema grouping for callers that bootstrap an older complete
# database before adding the resumable-stage tables. It has no migration or
# data-rewrite behavior.
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
    """Create missing tables without migrating, deleting, or rewriting data.

    Historical schema conversion was deliberately removed from the normal
    runtime path. A database with an incompatible legacy schema now fails
    loudly and must be migrated or reinitialized by an explicit operator
    action; opening a draft must never mutate the published report store.
    """

    Base.metadata.create_all(bind=engine)
    _assert_supported_schema(engine)


def _assert_supported_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    actual_tables = {name for name in inspector.get_table_names() if not name.startswith("sqlite_")}
    expected_tables = set(Base.metadata.tables)
    unexpected = actual_tables - expected_tables
    missing_columns: dict[str, set[str]] = {}
    for table_name in expected_tables & actual_tables:
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = set(Base.metadata.tables[table_name].columns.keys())
        missing = expected_columns - actual_columns
        if missing:
            missing_columns[table_name] = missing
    if not unexpected and not missing_columns:
        return
    details: list[str] = []
    if unexpected:
        details.append("unexpected tables=" + ",".join(sorted(unexpected)))
    details.extend(
        f"{table} missing columns={','.join(sorted(columns))}"
        for table, columns in sorted(missing_columns.items())
    )
    raise RuntimeError(
        "Existing database schema is unsupported by the current pipeline; "
        "no conversion is available and no automatic migration is performed: "
        + "; ".join(details)
        + ". Migrate or reinitialize it explicitly."
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
