from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.storage.read_repository import AllItemFilters, UIReadRepository
from app.web.deps import get_repository, templates


router = APIRouter()


@router.get("/dynamics")
def all_dynamics(
    request: Request,
    q: str | None = None,
    source_group: str | None = None,
    content_class: str | None = None,
    topic_category: str | None = None,
    edition_date: str | None = None,
    page: int = Query(default=1, ge=1),
    repo: UIReadRepository = Depends(get_repository),
):
    active_edition = repo.resolve_edition(edition_date=edition_date)
    filters = AllItemFilters(
        query=q,
        source_group=source_group,
        content_class=content_class,
        topic_category=topic_category,
    )
    page_size = 50
    items, total_count = repo.list_all_dynamics(
        edition=active_edition,
        query=filters.query,
        source_group=filters.source_group,
        content_class=filters.content_class,
        topic_category=filters.topic_category,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    filter_options = repo.list_filter_options()
    return templates.TemplateResponse(
        request=request,
        name="dynamics.html",
        context={
            "request": request,
            "active_nav": "dynamics",
            "items": items,
            "total_count": total_count,
            "filters": filters,
            "filter_options": filter_options,
            "active_edition": active_edition,
            "active_edition_date": active_edition.edition_date if active_edition else None,
            "edition_options": repo.list_daily_editions(),
            "page": page,
            "has_next_page": total_count > page * page_size,
        },
    )
