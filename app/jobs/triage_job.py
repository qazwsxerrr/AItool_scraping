"""Deterministic prefilter plus structured AI triage stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.client import ItemAnalysisClient
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, load_source_registry
from app.domain.models import SourceSpec
from app.pipeline.triage import build_triage_request, triage_item
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.event_repository import EventRepository
from app.storage.models import IntelItem, Source
from app.storage.repository import IntelRepository


@dataclass
class TriageJobResult:
    processed: int = 0
    kept: int = 0
    filtered: int = 0
    ai_success: int = 0
    ai_failed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_triage_job(
    *,
    session_factory: sessionmaker[Session],
    source_specs: Mapping[str, SourceSpec] | None = None,
    ai_client: ItemAnalysisClient | Any | None = None,
    limit: int | None = 100,
    source_filter: str | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> TriageJobResult:
    result = TriageJobResult()
    specs = dict(source_specs or {})
    current = now or datetime.now(timezone.utc)
    with session_factory() as session:
        stmt = select(IntelItem).order_by(IntelItem.id.asc())
        if source_filter:
            stmt = stmt.where(IntelItem.source_id == source_filter)
        if not force:
            stmt = stmt.where(IntelItem.status.in_(["new", "selected", "ai_failed", "hotspot"]))
        if limit is not None:
            stmt = stmt.limit(limit)
        items = list(session.scalars(stmt).all())
        repo = IntelRepository(session)
        event_repo = EventRepository(session)
        for item in items:
            result.processed += 1
            try:
                source = specs.get(item.source_id) or _spec_from_row(item.source)
                values = _item_values(item)
                deterministic = triage_item(values, source, now=current)
                deterministic_payload = _triage_payload(deterministic)
                if not deterministic.keep:
                    event_repo.upsert_triage_review(
                        item.id,
                        deterministic_payload,
                        model=None,
                        raw_response=deterministic_payload,
                        status="filtered",
                        deterministic_score={"score": deterministic.deterministic_score, "reason": deterministic.reason},
                    )
                    repo.set_item_status(item.id, "filtered")
                    result.filtered += 1
                    session.commit()
                    continue
                call = ai_client.triage_item(build_triage_request(values)) if ai_client is not None else None
                if call is None:
                    payload = deterministic_payload
                    status = "not_configured"
                    error = "triage AI client is not configured"
                elif getattr(call, "ok", False):
                    merged = triage_item(values, source, now=current, response=call.parsed)
                    payload = _triage_payload(merged, call.parsed)
                    status = "success"
                    error = None
                    result.ai_success += 1
                else:
                    payload = deterministic_payload
                    status = getattr(call, "status", "ai_failed")
                    error = getattr(call, "error", None) or "triage AI failed"
                    result.ai_failed += 1
                event_repo.upsert_triage_review(
                    item.id,
                    payload,
                    model=getattr(call, "model", None) if call is not None else None,
                    raw_response=getattr(call, "raw", None) if call is not None else payload,
                    status=status,
                    error_message=error,
                    deterministic_score={"score": deterministic.deterministic_score, "reason": deterministic.reason},
                )
                keep = bool(payload.get("keep")) and status == "success"
                repo.set_item_status(item.id, "selected" if keep else ("ai_failed" if status != "success" else "filtered"))
                result.kept += int(keep)
                result.filtered += int(not keep)
                session.commit()
            except Exception as exc:
                session.rollback()
                result.failed += 1
                result.errors.append(f"intel_item_id={item.id}: {exc}")
    return result


def run_triage_from_settings(
    *, settings: Settings, registry_path=DEFAULT_REGISTRY_PATH, **kwargs: Any
) -> TriageJobResult:
    registry = load_source_registry(registry_path, env={"RSSHUB_BASE_URL": settings.rsshub_base_url or ""})
    specs = {source.id: source for source in registry.sources}
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    client = ItemAnalysisClient.from_settings(settings)
    return run_triage_job(session_factory=create_session_factory(engine), source_specs=specs, ai_client=client, **kwargs)


def _item_values(item: IntelItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "item_id": item.id,
        "source_id": item.source_id,
        "title": item.title,
        "original_title": item.original_title or item.title,
        "canonical_url": item.canonical_url,
        "source_url": item.source_url or item.canonical_url,
        "published_at": item.published_at,
        "discovered_at": item.discovered_at or item.captured_at,
        "content_text": item.content_text,
        "summary": item.summary,
        "content_class": item.content_class,
        "event_hint": item.event_hint,
    }


def _spec_from_row(row: Source | None) -> SourceSpec:
    if row is None:
        raise ValueError("source row is missing")
    data: dict[str, Any] = {
        "id": row.id, "name": row.name, "transport": row.transport, "url": row.url,
        "enabled": row.enabled, "priority": row.priority, "fetch_interval": row.fetch_interval,
        "default_limit": row.default_limit, "source_group": row.source_group, "source_subtype": row.source_subtype,
        "tier": row.tier, "topic_scopes": row.topic_scopes, "primary_eligible": row.primary_eligible,
        "citation_policy": row.citation_policy, "account_verification_url": row.account_verification_url,
        "content_class": row.content_class,
    }
    if row.transport in {"feed", "rsshub"}:
        data["feed"] = {"format": row.feed_format or "rss", "adapter": row.feed_adapter or "generic"}
    elif row.transport == "github":
        data["github"] = {"mode": row.github_mode or "search", "query": row.github_query, "sort": row.github_sort or "updated", "order": row.github_order or "desc", "pushed_days": row.github_pushed_days, "period": row.github_period}
    return SourceSpec.model_validate(data)


def _triage_payload(result: Any, response: Any | None = None) -> dict[str, Any]:
    payload = result.__dict__.copy()
    payload["risk_flags"] = list(payload.get("risk_flags") or [])
    payload["claim_types"] = list(payload.get("claim_types") or [])
    payload["entities"] = list(getattr(response, "entities", []) or [])
    return payload


__all__ = ["TriageJobResult", "run_triage_job", "run_triage_from_settings"]
