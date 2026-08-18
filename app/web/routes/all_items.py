from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.storage.read_repository import AllItemFilters, UIReadRepository
from app.web.deps import get_repository, templates


router = APIRouter()


@router.get("/all")
def all_items(
    request: Request,
    q: str | None = None,
    source_group: str | None = None,
    content_class: str | None = None,
    topic_category: str | None = None,
    run_date: str | None = None,
    page: int = Query(default=1, ge=1),
    repo: UIReadRepository = Depends(get_repository),
):
    active_snapshot = repo.resolve_snapshot(edition_date=run_date)
    filters = AllItemFilters(
        query=q,
        source_group=source_group,
        content_class=content_class,
        topic_category=topic_category,
    )
    page_size = 50
    rows = repo.list_featured_cards(
        snapshot=active_snapshot,
        query=filters.query,
        source_group=filters.source_group,
        content_class=filters.content_class,
        category=filters.topic_category,
        offset=(page - 1) * page_size,
        limit=page_size + 1,
    )
    items = rows[:page_size]
    filter_options = repo.list_filter_options()
    return templates.TemplateResponse(
        request=request,
        name="all_items.html",
        context={
            "request": request,
            "active_nav": "all",
            "items": items,
            "filters": filters,
            "filter_options": filter_options,
            "active_snapshot": active_snapshot,
            "active_edition_date": active_snapshot.edition_date if active_snapshot else None,
            "edition_options": repo.list_daily_editions(),
            "page": page,
            "has_next_page": len(rows) > page_size,
        },
    )
