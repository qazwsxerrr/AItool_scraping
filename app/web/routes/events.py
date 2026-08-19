"""Selected-event detail pages backed by one resolved daily edition."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request

from app.storage.read_repository import UIReadRepository
from app.web.deps import get_repository, templates


router = APIRouter()


@router.get("/events/{event_id}")
def event_detail(
    request: Request,
    event_id: int,
    run_date: str | None = None,
    origin: str | None = None,
    return_q: str | None = None,
    repo: UIReadRepository = Depends(get_repository),
):
    snapshot = repo.resolve_snapshot(edition_date=run_date)
    detail = repo.get_selected_event_detail(event_id, snapshot=snapshot)
    if detail is None:
        raise HTTPException(status_code=404, detail="本期快照中不存在该入选事件")
    active_nav, back_url, back_label = _navigation_context(
        origin=origin,
        edition_date=snapshot.edition_date if snapshot is not None else None,
        return_q=return_q,
    )
    return templates.TemplateResponse(
        request=request,
        name="event_detail.html",
        context={
            "request": request,
            "active_nav": active_nav,
            "detail": detail,
            "active_snapshot": snapshot,
            "active_edition_date": snapshot.edition_date if snapshot is not None else None,
            "edition_options": repo.list_daily_editions(),
            "back_url": back_url,
            "back_label": back_label,
        },
    )


def _navigation_context(
    *,
    origin: str | None,
    edition_date: str | None,
    return_q: str | None,
) -> tuple[str | None, str, str]:
    """Keep detail navigation anchored to the public page that opened it."""

    origin = origin if origin in {"home", "all", "search"} else None
    destination = {"home": "/", "all": "/all", "search": "/search"}.get(origin, "/all")
    params: dict[str, str] = {}
    if edition_date:
        params["run_date"] = edition_date
    if origin == "search" and return_q:
        params["q"] = return_q
    back_url = f"{destination}?{urlencode(params)}" if params else destination
    labels = {
        "home": "返回今日精选",
        "all": "返回本期精选",
        "search": "返回热点搜索",
    }
    return origin, back_url, labels.get(origin, "返回本期精选")
