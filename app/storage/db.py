from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import Base


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
    Base.metadata.create_all(bind=engine)
    _apply_additive_schema_updates(engine)


def _apply_additive_schema_updates(engine: Engine) -> None:
    """Install additive SQLite columns without deleting existing local data.

    The project intentionally has no migration framework.  ``create_all``
    does not alter an already-existing table, so triage fields are installed
    one column at a time with safe defaults for old rows.  ``topic_category``
    is retained as a compatibility column used by the dirty main worktree.
    """

    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "ai_item_reviews" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("ai_item_reviews")}
    additions = {
        "topic": "VARCHAR(32)",
        "topics_json": "TEXT NOT NULL DEFAULT '[]'",
        "keywords_json": "TEXT NOT NULL DEFAULT '[]'",
        "selection_score": "INTEGER",
        "scores_json": "TEXT NOT NULL DEFAULT '{}'",
        "novelty": "VARCHAR(16)",
        "novelty_score": "INTEGER NOT NULL DEFAULT 0",
        "paper_support_json": "TEXT NOT NULL DEFAULT '{}'",
        "topic_category": "VARCHAR(128)",
    }
    missing = [(name, definition) for name, definition in additions.items() if name not in columns]
    if not missing:
        return
    with engine.begin() as connection:
        for name, definition in missing:
            connection.execute(
                text(f"ALTER TABLE ai_item_reviews ADD COLUMN {name} {definition}")
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
