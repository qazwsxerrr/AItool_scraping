"""Read-only date-addressed daily-edition JSON API for the public UI."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder

from app.storage.read_repository import UIReadRepository
from app.web.deps import get_repository


router = APIRouter(prefix="/api", tags=["intelligence"])


@router.get("/ui/current")
def current_ui(
    edition_date: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    repo: UIReadRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Return the selected event feed for the resolved public daily edition."""

    snapshot = repo.resolve_snapshot(edition_date=edition_date)
    summary = repo.get_run_snapshot_summary(snapshot)
    return _response(
        snapshot=snapshot,
        stats=repo.get_dashboard_stats(snapshot=snapshot) if snapshot is not None else None,
        editions=repo.list_daily_editions(),
        events=repo.list_featured_events(snapshot=snapshot, limit=limit),
        **_summary_fields(summary),
    )


@router.get("/editions")
def list_editions(
    repo: UIReadRepository = Depends(get_repository),
) -> dict[str, Any]:
    """List the public report for each daily edition."""

    editions = repo.list_daily_editions()
    return {"items": _encode(editions), "count": len(editions)}


@router.get("/editions/{edition_date}/events")
def edition_events(
    edition_date: str,
    topic: str | None = None,
    content_class: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    repo: UIReadRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Read selected events from one public date-addressed daily report."""

    snapshot = _require_edition(repo, edition_date=edition_date)
    events = repo.list_featured_events(
        snapshot=snapshot,
        topic=topic,
        content_class=content_class,
        limit=limit,
    )
    summary = repo.get_run_snapshot_summary(snapshot)
    return _response(
        snapshot=snapshot,
        events=events,
        **_summary_fields(summary),
    )


@router.get("/editions/{edition_date}/events/{event_id}")
def edition_event_detail(
    edition_date: str,
    event_id: int,
    repo: UIReadRepository = Depends(get_repository),
) -> dict[str, Any]:
    """Read one selected event and its retained public provenance."""

    snapshot = _require_edition(repo, edition_date=edition_date)
    detail = repo.get_selected_event_detail(event_id, snapshot=snapshot)
    if detail is None:
        raise HTTPException(status_code=404, detail="Selected event not found in this snapshot")
    return {
        "snapshot": _encode(snapshot),
        "event": _encode(detail),
    }


def _require_edition(
    repo: UIReadRepository,
    *,
    edition_date: str,
):
    snapshot = repo.resolve_snapshot(edition_date=edition_date)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Daily snapshot not found")
    return snapshot


def _response(**values: Any) -> dict[str, Any]:
    events = values.pop("events", ())
    payload = {key: _encode(value) for key, value in values.items()}
    payload["events"] = _encode(events)
    payload["count"] = len(events)
    return payload


def _summary_fields(summary: dict[str, Any] | None) -> dict[str, Any]:
    if summary is None:
        return {"funnel": None, "stages": {}, "failure_reasons": []}
    return {
        "funnel": summary.get("funnel", {}),
        "stages": summary.get("stages", {}),
        "failure_reasons": summary.get("failure_reasons", []),
    }


def _encode(value: Any) -> Any:
    if value is None:
        return None
    encoded = jsonable_encoder(asdict(value) if hasattr(value, "__dataclass_fields__") else value)
    return _strip_internal_run_fields(encoded)


def _strip_internal_run_fields(value: Any) -> Any:
    """Keep durable run identifiers out of the public read API."""

    if isinstance(value, dict):
        return {
            key: _strip_internal_run_fields(child)
            for key, child in value.items()
            if not _is_internal_execution_key(key)
        }
    if isinstance(value, list):
        return [_strip_internal_run_fields(child) for child in value]
    return value


def _is_internal_execution_key(key: object) -> bool:
    normalized = str(key).strip().casefold()
    return (
        normalized == "run_id"
        or normalized.endswith("_run_id")
        or normalized == "snapshot_key"
        or normalized.endswith("_snapshot_key")
    )
