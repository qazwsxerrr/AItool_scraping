from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, text
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
    _ensure_sqlite_compat_columns(engine)


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


def _ensure_sqlite_compat_columns(engine: Engine) -> None:
    """Add additive SQLite columns introduced after the MVP schema.

    The project intentionally avoids a migration framework in the MVP.  SQLite
    create_all() will not alter existing tables, so keep small additive changes
    here to let local databases keep running after model extensions.
    """
    if not engine.dialect.name.startswith("sqlite"):
        return

    required_columns: dict[str, dict[str, str]] = {
        "sources": {
            "source_group": "VARCHAR(64) NOT NULL DEFAULT 'general'",
            "source_subtype": "VARCHAR(64) NOT NULL DEFAULT 'fixed'",
            "quality_weight": "FLOAT",
            "source_role": "VARCHAR(64)",
            "spam_risk": "VARCHAR(32)",
            "requires_verification": "BOOLEAN",
        },
        "extracted_claims": {
            "evidence_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
            "evidence_attempts": "INTEGER NOT NULL DEFAULT 0",
            "evidence_error": "TEXT",
            "evidence_searched_at": "DATETIME",
        },
        "evidence_items": {
            "retrieval_score": "INTEGER NOT NULL DEFAULT 0",
            "evidence_confidence": "INTEGER NOT NULL DEFAULT 0",
            "http_status": "INTEGER",
            "final_url": "TEXT",
            "url_validation_status": "VARCHAR(32) NOT NULL DEFAULT 'unchecked'",
            "fetched_title": "TEXT",
            "fetched_description": "TEXT",
            "fetched_text_preview": "TEXT",
            "fetch_status": "VARCHAR(32) NOT NULL DEFAULT 'pending'",
            "fetch_error": "TEXT",
            "risk_flags": "TEXT NOT NULL DEFAULT '[]'",
            "quality_flags": "TEXT NOT NULL DEFAULT '[]'",
        },
        "verification_items": {
            "freshness_score": "INTEGER NOT NULL DEFAULT 0",
        },
        "canonical_entities": {
            "last_recommended_at": "DATETIME",
            "last_update_reason": "TEXT",
            "major_update_detected": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "user_feedback": {
            "entity_id": "INTEGER",
            "candidate_item_id": "INTEGER",
            "action": "VARCHAR(32)",
            "reason": "TEXT",
            "created_at": "DATETIME",
        },
    }

    with engine.begin() as connection:
        for table_name, columns in required_columns.items():
            table_exists = connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name"),
                {"table_name": table_name},
            ).first()
            if table_exists is None:
                continue

            existing = {
                row[1]
                for row in connection.execute(text(f"PRAGMA table_info({table_name})")).all()
            }
            for column_name, column_sql in columns.items():
                if column_name in existing:
                    continue
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))
