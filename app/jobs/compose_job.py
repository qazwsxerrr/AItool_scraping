"""Compose quota-balanced events and persist structured editorial output."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.client import ItemAnalysisClient
from app.config.daily_profile import load_daily_profile
from app.config.settings import Settings
from app.pipeline.editorial import compose_daily_selection, event_score
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.edition_repository import EditionRepository
from app.storage.event_repository import EventRepository
from app.storage.models import Event, Source


@dataclass
class ComposeResult:
    candidates: int = 0
    selected: int = 0
    written: int = 0
    failed: int = 0
    event_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_compose_job(*, session_factory: sessionmaker[Session], ai_client: ItemAnalysisClient | Any | None = None, limit: int | None = 200, force: bool = False, now: datetime | None = None, profile: Any | None = None) -> ComposeResult:
    result = ComposeResult(); current = now or datetime.now(timezone.utc); profile = profile or load_daily_profile()
    with session_factory() as session:
        events = list(session.scalars(select(Event).where(Event.state.in_(["candidate", "composed"])).order_by(Event.score.desc(), Event.id.asc()).limit(limit or 200)).all())
        result.candidates = len(events)
        rows = [_event_values(event, session.get(Source, event.primary_source_id)) for event in events]
        selected = compose_daily_selection(rows, profile, now=current)
        result.selected = len(selected)
        repo = EventRepository(session)
        used_ids = {int(row["id"]) for row in selected}
        for row in selected:
            try:
                event = session.get(Event, int(row["id"])); evidence = repo.list_event_evidence(event.id) if event else []
                if event is None: continue
                if ai_client is not None:
                    call = ai_client.write_event(row, [{"id": e.evidence_key, "citation_url": e.citation_url} for e in evidence])
                    if not getattr(call, "ok", False):
                        repo.upsert_editorial_review(
                            event.id,
                            None,
                            model=getattr(call, "model", None),
                            raw_response=getattr(call, "raw", None),
                            status=getattr(call, "status", "request_error"),
                            error_message=getattr(call, "error", None) or "write_event failed",
                        )
                        session.commit()
                        fallback = next((candidate for candidate in rows if candidate.get("section") == row.get("section") and int(candidate.get("id") or 0) not in used_ids), None)
                        if fallback is not None:
                            fallback_event = session.get(Event, int(fallback["id"]))
                            fallback_evidence = repo.list_event_evidence(fallback_event.id) if fallback_event else []
                            fallback_call = ai_client.write_event(fallback, [{"id": e.evidence_key, "citation_url": e.citation_url} for e in fallback_evidence]) if fallback_event is not None else None
                            if fallback_event is not None and getattr(fallback_call, "ok", False):
                                repo.upsert_editorial_review(fallback_event.id, fallback_call.parsed, model=getattr(fallback_call, "model", None), raw_response=getattr(fallback_call, "raw", None), status="success")
                                fallback_event.state = "composed"; fallback_event.score = event_score(fallback, now=current)
                                used_ids.add(fallback_event.id); result.written += 1; result.event_ids.append(fallback_event.id); session.commit(); continue
                        result.failed += 1; result.errors.append(f"event_id={event.id}: {getattr(call, 'error', 'write failed')}"); continue
                    repo.upsert_editorial_review(event.id, call.parsed, model=getattr(call, "model", None), raw_response=getattr(call, "raw", None), status="success")
                event.state = "composed"; event.score = event_score(row, now=current)
                result.written += 1; result.event_ids.append(event.id)
                session.commit()
            except Exception as exc:
                session.rollback(); result.failed += 1; result.errors.append(f"event_id={row.get('id')}: {exc}")
    return result


def run_compose_from_settings(*, settings: Settings, **kwargs: Any) -> ComposeResult:
    engine = create_engine_from_url(settings.database_url); init_db(engine)
    return run_compose_job(session_factory=create_session_factory(engine), ai_client=ItemAnalysisClient.from_settings(settings), **kwargs)


def _event_values(event: Event, source: Source | None = None) -> dict[str, Any]:
    source_id = event.primary_source_id
    return {"id": event.id, "event_id": event.id, "section": event.section, "event_type": event.event_type, "event_hint": event.event_hint, "title": event.title, "primary_source_id": source_id, "source_id": source_id, "source_group": source.source_group if source else None, "tier": source.tier if source else "p4", "primary_eligible": source.primary_eligible if source else False, "citation_policy": source.citation_policy if source else "discovery_only", "discovered_at": event.discovered_at, "score": event.score, "primary_document": event.primary_document_id}


__all__ = ["ComposeResult", "run_compose_job", "run_compose_from_settings"]
