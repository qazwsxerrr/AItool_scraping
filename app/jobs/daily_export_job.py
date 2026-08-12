"""Render a date-scoped daily edition and auditable JSONL artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config.daily_profile import load_daily_profile
from app.config.settings import Settings
from app.pipeline.editorial import evaluate_publication_gates
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.edition_repository import EditionRepository
from app.storage.event_repository import EventRepository
from app.storage.models import Document, Event, EventEditorialReview, Source


@dataclass
class DailyExportResult:
    edition_date: str
    status: str
    selected: int
    published: bool
    markdown_path: str
    events_path: str
    draft_path: str | None = None
    pending_path: str | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)


def run_daily_export_job(
    *, session_factory: sessionmaker[Session], edition_date: date | str | None = None,
    output_dir: str | Path = "output/daily", force: bool = False, profile: Any | None = None,
) -> DailyExportResult:
    day = _as_date(edition_date); profile = profile or load_daily_profile(); root = Path(output_dir) / f"{day:%Y}" / f"{day:%m}"; root.mkdir(parents=True, exist_ok=True)
    markdown = root / f"{day.isoformat()}.md"; events_path = root / f"{day.isoformat()}.events.jsonl"; draft = root / f"{day.isoformat()}.draft.md"; pending = root / f"{day.isoformat()}.pending.jsonl"
    with session_factory() as session:
        edition_repo = EditionRepository(session); existing = edition_repo.get_edition(day)
        if existing is not None and existing.status == "published" and not force:
            return DailyExportResult(day.isoformat(), "published", len(_load_events(existing.events_json)), True, str(markdown), str(events_path), failures=[])
        events = list(session.scalars(select(Event).where(Event.state == "composed").order_by(Event.score.desc(), Event.id.asc())).all())
        reviews = {review.event_id: review for review in session.scalars(select(EventEditorialReview).where(EventEditorialReview.event_id.in_([e.id for e in events] or [-1]))).all()}
        rows = [
            _event_values(
                event,
                reviews.get(event.id),
                session.get(Source, event.primary_source_id),
                session.get(Document, event.primary_document_id),
            )
            for event in events
        ]
        gates = evaluate_publication_gates(rows, profile, editorial_reviews=reviews)
        status = "published" if gates.publishable else "blocked"
        edition = edition_repo.upsert_edition(day, status=status, gate_results=gates.to_dict(), markdown_path=str(markdown if gates.publishable else draft), events=rows)
        edition_repo.set_entries(edition.id, rows)
        session.commit()
        jsonl = "".join(json.dumps(row, ensure_ascii=False, default=str, sort_keys=True) + "\n" for row in rows)
        if gates.publishable:
            markdown.write_text(_render_markdown(day, rows), encoding="utf-8"); events_path.write_text(jsonl, encoding="utf-8")
            return DailyExportResult(day.isoformat(), status, len(rows), True, str(markdown), str(events_path), failures=[])
        draft.write_text(_render_draft(day, rows, gates.gate_failures), encoding="utf-8"); pending.write_text(jsonl, encoding="utf-8")
        return DailyExportResult(day.isoformat(), status, len(rows), False, str(markdown), str(events_path), str(draft), str(pending), gates.gate_failures)


def run_daily_export_from_settings(*, settings: Settings, **kwargs: Any) -> DailyExportResult:
    engine = create_engine_from_url(settings.database_url); init_db(engine)
    return run_daily_export_job(session_factory=create_session_factory(engine), **kwargs)


def _as_date(value: date | str | None) -> date:
    if value is None: return datetime.now(timezone.utc).date()
    if isinstance(value, datetime): return value.date()
    if isinstance(value, date): return value
    return date.fromisoformat(str(value)[:10])


def _event_values(
    event: Event,
    review: Any | None,
    source: Source | None = None,
    document: Document | None = None,
) -> dict[str, Any]:
    source_id = event.primary_source_id
    source_group = source.source_group if source is not None else None
    return {
        "id": event.id,
        "event_id": event.id,
        "title": getattr(review, "title", None) or event.title,
        "summary_cn": getattr(review, "summary_cn", None),
        "why_it_matters": getattr(review, "why_it_matters", None),
        "section": event.section,
        "event_type": event.event_type,
        "event_hint": event.event_hint,
        "primary_source_id": source_id,
        "source_id": source_id,
        "source_group": source_group,
        "tier": source.tier if source is not None else ("p1" if event.section == "model_product" else "p2"),
        "primary_eligible": bool(source.primary_eligible) if source is not None else True,
        "citation_policy": source.citation_policy if source is not None else "primary",
        "source": source,
        "document": document,
        "primary_document": document,
        "primary_document_id": event.primary_document_id,
        "discovered_at": event.discovered_at,
        "editorial_review": {
            "status": getattr(review, "status", None),
            "title": getattr(review, "title", None),
            "facts": _json(getattr(review, "facts_json", None)),
        }
        if review
        else None,
        "score": event.score,
    }


def _render_markdown(day: date, rows: list[dict[str, Any]]) -> str:
    lines = [f"# AI 情报日报（{day.isoformat()}）", ""]
    current = None
    for index, row in enumerate(rows, 1):
        if row.get("section") != current:
            current = row.get("section"); lines.extend([f"## {current or '未分类'}", ""])
        lines.extend([f"### {index}. {row.get('title') or '(untitled)'}", "", row.get("summary_cn") or row.get("event_hint") or "暂无摘要", ""])
    return "\n".join(lines) + "\n"


def _render_draft(day: date, rows: list[dict[str, Any]], failures: list[dict[str, Any]]) -> str:
    return _render_markdown(day, rows) + "\n> BLOCKED\n\n" + "\n".join(f"- `{failure.get('code')}` {failure.get('message')}" for failure in failures) + "\n"


def _load_events(value: str) -> list[Any]:
    try: return json.loads(value or "[]")
    except (TypeError, ValueError): return []


def _json(value: str | None) -> Any:
    try: return json.loads(value or "[]")
    except (TypeError, ValueError): return []


__all__ = ["DailyExportResult", "run_daily_export_job", "run_daily_export_from_settings"]
