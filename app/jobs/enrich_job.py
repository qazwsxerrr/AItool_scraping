"""Idempotent minimal document enrichment stage for the daily pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.pipeline.enrich import enrich_item
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.event_repository import EventRepository
from app.storage.models import Document, IntelItem


@dataclass
class EnrichResult:
    processed: int = 0
    enriched: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_enrich_job(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None = 100,
    source_filter: str | None = None,
    force: bool = False,
    fetcher: Callable[[str], Any] | None = None,
    now: datetime | None = None,
) -> EnrichResult:
    result = EnrichResult()
    with session_factory() as session:
        stmt = select(IntelItem).order_by(IntelItem.id.asc())
        if source_filter:
            stmt = stmt.where(IntelItem.source_id == source_filter)
        if limit is not None:
            stmt = stmt.limit(limit)
        items = list(session.scalars(stmt).all())
        repo = EventRepository(session)
        for item in items:
            result.processed += 1
            existing = session.scalar(select(Document).where(Document.item_id == item.id))
            if existing is not None and not force:
                result.skipped += 1
                continue
            try:
                snapshot = enrich_item(item, fetcher=fetcher, now=now)
                doc = repo.upsert_document(
                    item_id=item.id,
                    source_id=item.source_id,
                    canonical_url=snapshot.canonical_url,
                    source_url=snapshot.source_url,
                    title=snapshot.title,
                    content_excerpt=snapshot.content_excerpt,
                    content_text=snapshot.content_text,
                    content_hash=snapshot.content_hash,
                    fetched_at=snapshot.fetched_at,
                    http_status=snapshot.http_status,
                    status=snapshot.status,
                    metadata=snapshot.metadata or {},
                )
                result.enriched += 1
                session.commit()
            except Exception as exc:
                session.rollback()
                result.failed += 1
                result.errors.append(f"intel_item_id={item.id}: {exc}")
    return result


def run_enrich_from_settings(
    *,
    settings: Settings,
    limit: int | None = 100,
    source_filter: str | None = None,
    force: bool = False,
    fetcher: Callable[[str], Any] | None = None,
    now: datetime | None = None,
) -> EnrichResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return run_enrich_job(
        session_factory=create_session_factory(engine),
        limit=limit,
        source_filter=source_filter,
        force=force,
        fetcher=fetcher,
        now=now or datetime.now(timezone.utc),
    )


__all__ = ["EnrichResult", "run_enrich_job", "run_enrich_from_settings"]
