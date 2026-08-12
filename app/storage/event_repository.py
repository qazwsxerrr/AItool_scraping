"""Write/read repository for V3 documents, events and editorial stages.

The legacy :mod:`app.storage.repository` remains the owner of v2 item fetch
and process writes.  This module is intentionally additive so daily jobs can
persist the event graph without coupling routes or templates to SQLAlchemy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.ai.schemas import ClusterDecision, EventEditorialResponse, TriageResponse
from app.storage.models import (
    ClusterDecision as ClusterDecisionRow,
    Document,
    Event,
    EventEditorialReview,
    EventEvidence,
    IntelItem,
    TriageReview,
    utcnow,
)


@dataclass(frozen=True)
class EventUpsertResult:
    event: Event
    created: bool

    @property
    def event_id(self) -> int:
        return self.event.id


@dataclass(frozen=True)
class EvidenceUpsertResult:
    evidence: EventEvidence
    created: bool

    @property
    def evidence_id(self) -> str:
        return self.evidence.evidence_key


class EventRepository:
    """Idempotent persistence boundary for the event-based daily pipeline."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_document(
        self,
        item: Any | None = None,
        *,
        item_id: int | None = None,
        source_id: str | None = None,
        canonical_url: str | None = None,
        source_url: str | None = None,
        title: str | None = None,
        content_excerpt: str | None = None,
        content_text: str | None = None,
        content_hash: str | None = None,
        fetched_at: datetime | None = None,
        http_status: int | None = None,
        status: str = "fetched",
        metadata: Mapping[str, Any] | None = None,
    ) -> Document:
        values = _mapping(item)
        item_id = item_id if item_id is not None else _int_or_none(values.get("item_id", values.get("id")))
        source_id = source_id or _text(values.get("source_id"))
        canonical_url = canonical_url or _text(values.get("canonical_url"))
        source_url = source_url or _text(values.get("source_url") or values.get("url") or values.get("link"))
        title = title or _text(values.get("title") or values.get("original_title"))
        content_excerpt = content_excerpt or _text(values.get("content_excerpt"))
        content_text = content_text or _text(values.get("content_text") or values.get("content"))
        content_hash = content_hash or _text(values.get("content_hash"))
        fetched_at = fetched_at or _as_utc(values.get("fetched_at")) or utcnow()
        http_status = http_status if http_status is not None else _int_or_none(values.get("http_status"))
        status = status or _text(values.get("status")) or "fetched"
        metadata = dict(metadata or values.get("metadata") or {})

        row = None
        if canonical_url:
            row = self.session.scalar(select(Document).where(Document.canonical_url == canonical_url))
        if row is None and item_id is not None:
            row = self.session.scalar(select(Document).where(Document.item_id == item_id))
        if row is None:
            row = Document(canonical_url=canonical_url)
            self.session.add(row)
        for name, value in {
            "item_id": item_id,
            "source_id": source_id,
            "canonical_url": canonical_url,
            "source_url": source_url,
            "title": title,
            "content_excerpt": content_excerpt,
            "content_text": content_text,
            "content_hash": content_hash,
            "fetched_at": fetched_at,
            "http_status": http_status,
            "status": status,
            "metadata_json": _dump_json(metadata),
        }.items():
            if value is not None or name in {"status", "metadata_json"}:
                setattr(row, name, value)
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def get_document(self, document_id: int) -> Document | None:
        return self.session.get(Document, document_id)

    def list_event_evidence(self, event_id: int) -> list[EventEvidence]:
        stmt = select(EventEvidence).where(EventEvidence.event_id == event_id).order_by(EventEvidence.id.asc())
        return list(self.session.scalars(stmt).all())

    save_document = upsert_document

    def upsert_triage_review(
        self,
        item_id: int,
        response: TriageResponse | Mapping[str, Any] | Any | None = None,
        *,
        model: str | None = None,
        raw_response: Any | None = None,
        status: str = "success",
        error_message: str | None = None,
        deterministic_score: Mapping[str, Any] | None = None,
    ) -> TriageReview:
        values = _mapping(response)
        row = self.session.scalar(select(TriageReview).where(TriageReview.item_id == item_id))
        if row is None:
            row = TriageReview(item_id=item_id)
            self.session.add(row)
        row.model = model or _text(values.get("model"))
        for name in ("keep", "section", "event_type", "event_hint", "impact_score", "novelty_score", "readability_score", "reason", "confidence"):
            if name in values:
                setattr(row, name, values[name])
        row.entities_json = _dump_json(values.get("entities", []))
        row.risk_flags_json = _dump_json(values.get("risk_flags", []))
        row.claim_types_json = _dump_json(values.get("claim_types", []))
        row.deterministic_score_json = _dump_json(deterministic_score or values.get("deterministic_score", {}))
        row.raw_response_json = _dump_json(raw_response if raw_response is not None else values.get("raw_response", values))
        row.status = status
        row.error_message = error_message[:4000] if error_message else None
        row.updated_at = utcnow()
        self.session.flush()
        return row

    save_triage_review = upsert_triage_review

    def upsert_event(
        self,
        event: Any | None = None,
        *,
        canonical_key: str | None = None,
        canonical_url: str | None = None,
        repository_release_key: str | None = None,
        arxiv_id: str | None = None,
        doi: str | None = None,
        section: str | None = None,
        event_type: str | None = None,
        event_hint: str | None = None,
        title: str | None = None,
        state: str | None = None,
        score: float | None = None,
        primary_item_id: int | None = None,
        primary_document_id: int | None = None,
        primary_source_id: str | None = None,
        discovered_at: datetime | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EventUpsertResult:
        values = _mapping(event)
        canonical_key = canonical_key or _text(values.get("canonical_key"))
        if not canonical_key:
            from app.pipeline.event_cluster import canonical_event_key

            canonical_key = canonical_event_key(values)
        row = self.session.scalar(select(Event).where(Event.canonical_key == canonical_key))
        created = row is None
        if row is None:
            row = Event(canonical_key=canonical_key)
            self.session.add(row)
        fields = {
            "canonical_url": canonical_url or _text(values.get("canonical_url")),
            "repository_release_key": repository_release_key or _text(values.get("repository_release_key")),
            "arxiv_id": arxiv_id or _text(values.get("arxiv_id")),
            "doi": doi or _text(values.get("doi")),
            "section": section or _text(values.get("section")),
            "event_type": event_type or _text(values.get("event_type")),
            "event_hint": event_hint or _text(values.get("event_hint")),
            "title": title or _text(values.get("title")),
            "state": state or _text(values.get("state")) or "candidate",
            "score": score if score is not None else values.get("score", 0.0),
            "primary_item_id": primary_item_id if primary_item_id is not None else _int_or_none(values.get("primary_item_id")),
            "primary_document_id": primary_document_id if primary_document_id is not None else _int_or_none(values.get("primary_document_id")),
            "primary_source_id": primary_source_id or _text(values.get("primary_source_id")),
            "discovered_at": discovered_at or _as_utc(values.get("discovered_at")),
            "window_start": window_start or _as_utc(values.get("window_start")),
            "window_end": window_end or _as_utc(values.get("window_end")),
            "metadata_json": _dump_json(metadata or values.get("metadata", {})),
        }
        for name, value in fields.items():
            if value is not None or name in {"state", "score", "metadata_json"}:
                setattr(row, name, value)
        row.updated_at = utcnow()
        self.session.flush()
        return EventUpsertResult(row, created)

    save_event = upsert_event

    def upsert_event_evidence(
        self,
        event_id: int,
        *,
        evidence_key: str | None = None,
        item_id: int | None = None,
        document_id: int | None = None,
        role: str = "supplementary",
        support_level: str = "supplementary",
        is_primary: bool = False,
        citation_url: str | None = None,
        claim_types: Iterable[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> EvidenceUpsertResult:
        if evidence_key is None:
            evidence_key = f"ev-{event_id}-{item_id or 0}-{document_id or 0}-{role}"
        row = self.session.scalar(select(EventEvidence).where(EventEvidence.evidence_key == evidence_key))
        created = row is None
        if row is None:
            row = EventEvidence(evidence_key=evidence_key, event_id=event_id)
            self.session.add(row)
        row.event_id = event_id
        row.item_id = item_id
        row.document_id = document_id
        row.role = role
        row.support_level = support_level
        row.is_primary = bool(is_primary)
        row.citation_url = citation_url
        row.claim_types_json = _dump_json(list(claim_types))
        row.metadata_json = _dump_json(metadata or {})
        self.session.flush()
        return EvidenceUpsertResult(row, created)

    add_evidence = upsert_event_evidence

    def upsert_cluster_decision(
        self,
        left_item_id: int,
        right_item_id: int,
        decision: ClusterDecision | Mapping[str, Any] | Any,
        *,
        model: str | None = None,
        raw_response: Any | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> ClusterDecisionRow:
        left, right = sorted((int(left_item_id), int(right_item_id)))
        pair_key = f"{left}:{right}"
        values = _mapping(decision)
        row = self.session.scalar(select(ClusterDecisionRow).where(ClusterDecisionRow.pair_key == pair_key))
        if row is None:
            row = ClusterDecisionRow(pair_key=pair_key, left_item_id=left, right_item_id=right)
            self.session.add(row)
        row.decision = _text(values.get("decision")) or "uncertain"
        row.confidence = max(0, min(100, _int_or_none(values.get("confidence")) or 0))
        row.reason = _text(values.get("reason"))
        row.canonical_event_hint = _text(values.get("canonical_event_hint"))
        row.model = model
        row.raw_response_json = _dump_json(raw_response if raw_response is not None else values)
        row.status = status
        row.error_message = error_message[:4000] if error_message else None
        row.updated_at = utcnow()
        self.session.flush()
        return row

    save_cluster_decision = upsert_cluster_decision

    def upsert_editorial_review(
        self,
        event_id: int,
        response: EventEditorialResponse | Mapping[str, Any] | Any | None = None,
        *,
        model: str | None = None,
        raw_response: Any | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> EventEditorialReview:
        values = _mapping(response)
        valid_ids = {
            row.evidence_key
            for row in self.session.scalars(select(EventEvidence).where(EventEvidence.event_id == event_id)).all()
        }
        facts = []
        for fact in values.get("facts", []) or []:
            fact_values = _mapping(fact)
            ids = [str(value) for value in fact_values.get("evidence_ids", []) if str(value) in valid_ids]
            if ids:
                facts.append({"text": _text(fact_values.get("text")) or "", "evidence_ids": ids})
        row = self.session.scalar(select(EventEditorialReview).where(EventEditorialReview.event_id == event_id))
        if row is None:
            row = EventEditorialReview(event_id=event_id)
            self.session.add(row)
        row.model = model
        row.title = _text(values.get("title"))
        row.summary_cn = _text(values.get("summary_cn"))
        row.why_it_matters = _text(values.get("why_it_matters"))
        row.facts_json = _dump_json(facts)
        row.risk_notes_json = _dump_json(values.get("risk_notes", []))
        row.uncertainties_json = _dump_json(values.get("uncertainties", []))
        row.tags_json = _dump_json(values.get("tags", []))
        row.valid_evidence_ids_json = _dump_json(sorted({evidence_id for fact in facts for evidence_id in fact["evidence_ids"]}))
        row.raw_response_json = _dump_json(raw_response if raw_response is not None else values)
        row.status = status if facts or status != "success" else "invalid_evidence"
        row.error_message = error_message[:4000] if error_message else None
        self.session.flush()
        return row

    save_editorial_review = upsert_editorial_review

    def get_event(self, event_id: int) -> Event | None:
        return self.session.get(Event, event_id)

    def get_event_by_key(self, canonical_key: str) -> Event | None:
        return self.session.scalar(select(Event).where(Event.canonical_key == canonical_key))

    def list_events(self, *, section: str | None = None, state: str | None = None, limit: int | None = None) -> list[Event]:
        stmt = select(Event).order_by(Event.score.desc(), Event.id.asc())
        if section:
            stmt = stmt.where(Event.section == section)
        if state:
            stmt = stmt.where(Event.state == state)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())


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


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


__all__ = ["EventRepository", "EventUpsertResult", "EvidenceUpsertResult"]
