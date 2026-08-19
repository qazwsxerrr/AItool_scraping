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

    edition = repo.resolve_edition(edition_date=edition_date)
    summary = repo.get_edition_summary(edition)
    return _response(
        edition=edition,
        stats=repo.get_dashboard_stats(edition=edition) if edition is not None else None,
        editions=repo.list_daily_editions(),
        events=repo.list_featured_events(edition=edition, limit=limit),
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

    edition = _require_edition(repo, edition_date=edition_date)
    events = repo.list_featured_events(
        edition=edition,
        topic=topic,
        content_class=content_class,
        limit=limit,
    )
    summary = repo.get_edition_summary(edition)
    return _response(
        edition=edition,
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

    edition = _require_edition(repo, edition_date=edition_date)
    detail = repo.get_selected_event_detail(event_id, edition=edition)
    if detail is None:
        raise HTTPException(status_code=404, detail="Selected event not found in this daily edition")
    return {
        "edition": _encode(edition),
        "event": _encode(detail),
    }


def _require_edition(
    repo: UIReadRepository,
    *,
    edition_date: str,
):
    edition = repo.resolve_edition(edition_date=edition_date)
    if edition is None:
        raise HTTPException(status_code=404, detail="Daily edition not found")
    return edition


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
    return jsonable_encoder(asdict(value) if hasattr(value, "__dataclass_fields__") else value)
