"""Selected-event detail pages backed by one resolved daily edition."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request

from app.storage.read_repository import UIReadRepository
from app.web.deps import get_repository, is_historical_edition, templates


router = APIRouter()


@router.get("/events/{event_id}")
def event_detail(
    request: Request,
    event_id: int,
    edition_date: str | None = None,
    origin: str | None = None,
    return_q: str | None = None,
    repo: UIReadRepository = Depends(get_repository),
):
    active_edition = repo.resolve_edition(edition_date=edition_date)
    detail = repo.get_selected_event_detail(event_id, edition=active_edition)
    if detail is None:
        raise HTTPException(status_code=404, detail="本期快照中不存在该入选事件")
    active_nav, back_url, back_label = _navigation_context(
        origin=origin,
        edition_date=active_edition.edition_date if active_edition is not None else None,
        return_q=return_q,
    )
    return templates.TemplateResponse(
        request=request,
        name="event_detail.html",
        context={
            "request": request,
            "active_nav": active_nav,
            "detail": detail,
            "active_edition": active_edition,
            "active_edition_date": active_edition.edition_date if active_edition is not None else None,
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
        params["edition_date"] = edition_date
    if origin == "search" and return_q:
        params["q"] = return_q
    back_url = f"{destination}?{urlencode(params)}" if params else destination
    is_hist = is_historical_edition(edition_date)
    labels = {
        "home": f"返回 {edition_date} 精选" if is_hist and edition_date else "返回今日精选",
        "all": f"返回 {edition_date} 精选" if is_hist and edition_date else "返回本期精选",
        "search": "返回热点搜索",
    }
    default_label = f"返回 {edition_date} 精选" if is_hist and edition_date else "返回本期精选"
    return origin, back_url, labels.get(origin, default_label)
