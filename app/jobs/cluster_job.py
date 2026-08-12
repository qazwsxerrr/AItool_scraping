"""Deterministic exact/fuzzy event clustering with optional AI judgements."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.client import ItemAnalysisClient
from app.config.settings import Settings
from app.pipeline.event_cluster import canonical_event_key, cluster_candidates
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.event_repository import EventRepository
from app.storage.models import Document, IntelItem, Source, TriageReview


@dataclass
class ClusterResult:
    processed: int = 0
    events: int = 0
    merged: int = 0
    uncertain: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_cluster_job(
    *, session_factory: sessionmaker[Session], ai_client: ItemAnalysisClient | Any | None = None,
    limit: int | None = 100, force: bool = False, now: datetime | None = None,
) -> ClusterResult:
    result = ClusterResult()
    current = now or datetime.now(timezone.utc)
    with session_factory() as session:
        stmt = select(IntelItem).where(IntelItem.status.in_(["selected", "verified", "hotspot", "discovery_only"]))
        stmt = stmt.order_by(IntelItem.selection_score.desc(), IntelItem.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        items = list(session.scalars(stmt).all())
        triage_rows = {
            row.item_id: row
            for row in session.scalars(
                select(TriageReview).where(TriageReview.item_id.in_([item.id for item in items] or [-1]))
            ).all()
        }
        documents = {
            row.item_id: row
            for row in session.scalars(
                select(Document).where(Document.item_id.in_([item.id for item in items] or [-1]))
            ).all()
        }
        candidates = [_candidate_values(item, triage_rows.get(item.id), documents.get(item.id)) for item in items]
        groups = cluster_candidates(candidates)
        event_repo = EventRepository(session)
        for group in groups:
            if not group:
                continue
            result.processed += len(group)
            try:
                primary = group[0]
                key = canonical_event_key(primary)
                section = primary.get("section") or _section_from_item(primary)
                event_type = primary.get("event_type") or "signal"
                event_hint = primary.get("event_hint") or primary.get("title")
                event_result = event_repo.upsert_event(
                    canonical_key=key,
                    canonical_url=primary.get("canonical_url"), section=section,
                    event_type=event_type, event_hint=event_hint, title=primary.get("title"),
                    state="candidate", score=float(primary.get("selection_score") or 0),
                    primary_item_id=primary.get("id"), primary_document_id=primary.get("document_id"), primary_source_id=primary.get("source_id"),
                    discovered_at=primary.get("discovered_at") or current,
                )
                event = event_result.event
                result.events += int(event_result.created)
                for value in group:
                    evidence = event_repo.upsert_event_evidence(
                        event.id, item_id=value.get("id"), role="primary" if value is primary else "supplementary",
                        support_level="direct" if value is primary else "supplementary",
                        is_primary=value is primary, citation_url=value.get("canonical_url"),
                    )
                if len(group) > 1 and ai_client is not None:
                    for left, right in zip(group, group[1:]):
                        call = ai_client.judge_cluster(left, right)
                        decision = call.parsed if getattr(call, "ok", False) else {"decision": "uncertain", "confidence": 0, "reason": getattr(call, "error", "AI failed")}
                        event_repo.upsert_cluster_decision(left["id"], right["id"], decision, model=getattr(call, "model", None), raw_response=getattr(call, "raw", None), status=getattr(call, "status", "success"))
                        decision_value = getattr(decision, "decision", None)
                        if decision_value is None and isinstance(decision, dict):
                            decision_value = decision.get("decision")
                        if decision_value == "merge":
                            result.merged += 1
                        else:
                            result.uncertain += 1
                session.commit()
            except Exception as exc:
                session.rollback(); result.failed += 1; result.errors.append(str(exc))
    return result


def run_cluster_from_settings(*, settings: Settings, **kwargs: Any) -> ClusterResult:
    engine = create_engine_from_url(settings.database_url); init_db(engine)
    return run_cluster_job(session_factory=create_session_factory(engine), ai_client=ItemAnalysisClient.from_settings(settings), **kwargs)


def _candidate_values(item: IntelItem, triage: TriageReview | None = None, document: Document | None = None) -> dict[str, Any]:
    source = item.source
    metrics = _json_dict(item.metrics_json)
    raw_payload = _json_dict(item.raw_payload_json)
    repository = _first_text(
        metrics.get("repository"), metrics.get("repo"), metrics.get("github_repo"),
        metrics.get("full_name"), metrics.get("canonical_project_key"),
        raw_payload.get("repository"), raw_payload.get("repo"), raw_payload.get("full_name"),
    )
    if not repository:
        repository = _repository_from_url(
            item.canonical_url or item.source_url or (source.url if source else None)
        )
    release = _first_text(
        metrics.get("release"), metrics.get("release_tag"), metrics.get("tag_name"),
        metrics.get("version"), raw_payload.get("release"), raw_payload.get("release_tag"),
        raw_payload.get("tag_name"), raw_payload.get("version"),
    )
    arxiv_id = _first_text(
        metrics.get("arxiv_id"), metrics.get("arxiv"), metrics.get("paper_id"),
        raw_payload.get("arxiv_id"), raw_payload.get("arxiv"), raw_payload.get("paper_id"),
    )
    doi = _first_text(metrics.get("doi"), raw_payload.get("doi"))
    return {
        "id": item.id,
        "source_id": item.source_id,
        "external_id": item.external_id,
        "source_group": source.source_group if source else None,
        "tier": source.tier if source else "p4",
        "primary_eligible": source.primary_eligible if source else False,
        "citation_policy": source.citation_policy if source else "discovery_only",
        "canonical_url": item.canonical_url,
        "title": item.title,
        "section": triage.section if triage else None,
        "event_type": triage.event_type if triage else None,
        "event_hint": (triage.event_hint if triage else None) or item.event_hint or item.title,
        "selection_score": item.selection_score,
        "published_at": item.published_at,
        "discovered_at": item.discovered_at or item.captured_at,
        "document_id": document.id if document else None,
        # Keep identity aliases at the top level: canonical_event_key is
        # deliberately independent of the persisted JSON payload shape.
        "repository": repository,
        "release": release,
        "arxiv_id": arxiv_id,
        "doi": doi,
        "metrics": metrics,
        "raw_payload": raw_payload,
    }


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("full_name") or value.get("name") or value.get("id")
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _repository_from_url(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "api.github.com" and len(parts) >= 3 and parts[0].casefold() == "repos":
        return "/".join(parts[1:3])
    if host.endswith("github.com") and len(parts) >= 2:
        return "/".join(parts[:2])
    return None


def _section_from_item(value: Mapping[str, Any]) -> str:
    text = str(value.get("title") or "").casefold()
    if any(token in text for token in ("paper", "research", "benchmark")): return "research"
    if any(token in text for token in ("github", "repo", "tool")): return "open_source_tool"
    return "practice_opinion"


__all__ = ["ClusterResult", "run_cluster_job", "run_cluster_from_settings"]
