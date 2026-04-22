from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.pipeline.normalize import normalize_raw_item
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import NormalizedItemRepository, RawItemRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class NormalizeJobResult:
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_normalize_job(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None = 100,
) -> NormalizeJobResult:
    """Normalize raw_items with status=new into normalized_items idempotently."""
    result = NormalizeJobResult()

    with session_factory() as session:
        raw_repo = RawItemRepository(session)
        normalized_repo = NormalizedItemRepository(session)
        raw_items = raw_repo.list_pending_for_normalization(limit=limit)

        for raw_item in raw_items:
            result.processed += 1
            try:
                normalized = normalize_raw_item(raw_item)
                insert_result = normalized_repo.insert_if_new(normalized)
                if insert_result.inserted:
                    raw_repo.mark_status(raw_item.id, "normalized")
                    result.inserted += 1
                else:
                    raw_repo.mark_status(raw_item.id, "duplicate")
                    result.skipped += 1
            except Exception as exc:  # isolate bad source items within the batch
                raw_repo.mark_status(raw_item.id, "normalize_failed")
                result.failed += 1
                error = f"raw_item_id={raw_item.id}: {exc}"
                result.errors.append(error)
                LOGGER.exception("Failed to normalize raw item %s", raw_item.id)

        session.commit()

    return result


def run_normalize_from_settings(*, settings: Settings, limit: int | None = 100) -> NormalizeJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_normalize_job(session_factory=session_factory, limit=limit)
