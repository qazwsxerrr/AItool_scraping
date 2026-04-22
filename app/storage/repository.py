from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.source_registry import SourceConfig
from app.parsers.feed_parser import ParsedFeedItem
from app.storage.models import RawItem, Source


@dataclass(frozen=True)
class InsertResult:
    inserted: bool
    reason: str | None = None
    item_id: int | None = None


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_source(self, source: SourceConfig) -> Source:
        existing = self.session.get(Source, source.id)
        if existing is None:
            existing = Source(id=source.id)
            self.session.add(existing)

        existing.name = source.name
        existing.type = source.type
        existing.url = source.url
        existing.enabled = source.enabled
        existing.priority = source.priority
        existing.fetch_interval = source.fetch_interval
        existing.parser_type = source.parser_type
        return existing

    def mark_fetched(self, source_id: str, fetched_at: datetime | None = None) -> None:
        source = self.session.get(Source, source_id)
        if source is None:
            return
        source.last_fetched_at = fetched_at or datetime.now(timezone.utc)


class RawItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_new(self, item: ParsedFeedItem) -> InsertResult:
        duplicate_reason = self._find_duplicate_reason(item)
        if duplicate_reason:
            return InsertResult(inserted=False, reason=duplicate_reason)

        raw_item = RawItem(
            source_id=item.source_id,
            external_id=item.external_id,
            title=item.title,
            link=item.link,
            author=item.author,
            published_at=_as_utc(item.published_at),
            raw_summary=item.raw_summary,
            raw_content=item.raw_content,
            raw_payload=json.dumps(item.raw_payload, ensure_ascii=False, default=str),
            content_hash=item.content_hash,
            status="new",
        )
        self.session.add(raw_item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=raw_item.id)

    def _find_duplicate_reason(self, item: ParsedFeedItem) -> str | None:
        if item.external_id:
            stmt = select(RawItem.id).where(
                RawItem.source_id == item.source_id,
                RawItem.external_id == item.external_id,
            )
            if self.session.execute(stmt).first():
                return "duplicate_external_id"

        if item.link:
            stmt = select(RawItem.id).where(
                RawItem.source_id == item.source_id,
                RawItem.link == item.link,
            )
            if self.session.execute(stmt).first():
                return "duplicate_link"

        stmt = select(RawItem.id).where(RawItem.content_hash == item.content_hash)
        if self.session.execute(stmt).first():
            return "duplicate_content_hash"
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
