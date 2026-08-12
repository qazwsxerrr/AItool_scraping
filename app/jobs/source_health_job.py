"""Read-only source health report and optional stale-state refresh."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import Source
from app.storage.repository import IntelRepository


@dataclass(frozen=True)
class SourceHealthRow:
    source_id: str
    status: str
    consecutive_failures: int
    error_code: str | None
    error_message: str | None
    next_fetch_at: datetime | None


def run_source_health_job(*, session_factory, source_filter: str | None = None) -> list[SourceHealthRow]:
    with session_factory() as session:
        rows = IntelRepository(session).list_source_health(source_id=source_filter)
        return [SourceHealthRow(row.id, row.health_status, int(row.consecutive_failures or 0), row.last_error_code, row.last_error_message, row.backoff_until) for row in rows]


def run_source_health_from_settings(*, settings: Settings, source_filter: str | None = None) -> list[SourceHealthRow]:
    engine = create_engine_from_url(settings.database_url); init_db(engine)
    return run_source_health_job(session_factory=create_session_factory(engine), source_filter=source_filter)


__all__ = ["SourceHealthRow", "run_source_health_job", "run_source_health_from_settings"]
