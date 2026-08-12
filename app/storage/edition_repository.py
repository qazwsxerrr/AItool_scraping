"""Idempotent daily edition persistence for V3 jobs."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.storage.models import DailyEdition, DailyEventEntry, Event, utcnow


class EditionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_edition(
        self,
        edition_date: date | datetime | str,
        *,
        profile_name: str = "default",
        status: str = "draft",
        gate_results: Mapping[str, Any] | None = None,
        publish_fingerprint: str | None = None,
        markdown_path: str | None = None,
        events: Iterable[Any] | None = None,
    ) -> DailyEdition:
        day = _as_date(edition_date)
        row = self.session.scalar(
            select(DailyEdition).where(
                DailyEdition.edition_date == day,
                DailyEdition.profile_name == profile_name,
            )
        )
        if row is None:
            row = DailyEdition(edition_date=day, profile_name=profile_name)
            self.session.add(row)
        row.status = status
        row.gate_results_json = _dump_json(gate_results or {})
        row.publish_fingerprint = publish_fingerprint or row.publish_fingerprint
        row.markdown_path = markdown_path or row.markdown_path
        if events is not None:
            row.events_json = _dump_json([_event_ref(event) for event in events])
        row.updated_at = utcnow()
        self.session.flush()
        return row

    save_edition = upsert_edition

    def get_edition(self, edition_date: date | datetime | str, *, profile_name: str = "default") -> DailyEdition | None:
        return self.session.scalar(
            select(DailyEdition).where(
                DailyEdition.edition_date == _as_date(edition_date),
                DailyEdition.profile_name == profile_name,
            )
        )

    def set_entries(self, edition_id: int, events: Iterable[Any], *, replace: bool = True) -> list[DailyEventEntry]:
        if replace:
            existing = self.session.scalars(select(DailyEventEntry).where(DailyEventEntry.edition_id == edition_id)).all()
            for row in existing:
                self.session.delete(row)
            self.session.flush()
        entries: list[DailyEventEntry] = []
        for position, event in enumerate(events, start=1):
            values = _mapping(event)
            event_id = values.get("event_id", values.get("id"))
            if event_id is None and isinstance(event, Event):
                event_id = event.id
            if event_id is None:
                continue
            row = DailyEventEntry(
                edition_id=edition_id,
                event_id=int(event_id),
                position=position,
                section=_text(values.get("section")),
                rendered_json=_dump_json(values.get("rendered") or values.get("rendered_json") or values),
                status=_text(values.get("status")) or "selected",
            )
            self.session.add(row)
            entries.append(row)
        self.session.flush()
        return entries

    save_entries = set_entries

    def upsert_entry(
        self,
        edition_id: int,
        event_id: int,
        *,
        position: int,
        section: str | None = None,
        rendered: Mapping[str, Any] | None = None,
        status: str = "selected",
    ) -> DailyEventEntry:
        row = self.session.scalar(
            select(DailyEventEntry).where(
                DailyEventEntry.edition_id == edition_id,
                DailyEventEntry.event_id == event_id,
            )
        )
        if row is None:
            row = DailyEventEntry(edition_id=edition_id, event_id=event_id, position=position)
            self.session.add(row)
        row.position = position
        row.section = section
        row.rendered_json = _dump_json(rendered or {})
        row.status = status
        self.session.flush()
        return row

    def list_entries(self, edition_id: int) -> list[DailyEventEntry]:
        stmt = select(DailyEventEntry).where(DailyEventEntry.edition_id == edition_id).order_by(DailyEventEntry.position.asc())
        return list(self.session.scalars(stmt).all())

    @staticmethod
    def fingerprint(events: Iterable[Any], *, profile_name: str = "default") -> str:
        payload = {"profile": profile_name, "events": [_event_ref(event) for event in events]}
        return sha256(_dump_json(payload).encode("utf-8")).hexdigest()


def _event_ref(value: Any) -> dict[str, Any]:
    values = _mapping(value)
    event_id = values.get("event_id", values.get("id"))
    if isinstance(value, Event):
        event_id = value.id
    return {
        "event_id": event_id,
        "section": _text(values.get("section")),
        "title": _text(values.get("title")),
    }


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


__all__ = ["EditionRepository"]
