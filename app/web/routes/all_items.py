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
    status: str | None = None,
    ai_keep: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    repo: UIReadRepository = Depends(get_repository),
):
    filters = AllItemFilters(
        query=q,
        source_group=source_group,
        content_class=content_class,
        topic_category=topic_category,
        status=status,
        ai_keep=_parse_bool(ai_keep),
    )
    items = repo.list_all_items(filters=filters, page=page, page_size=50)
    filter_options = repo.list_filter_options()
    return templates.TemplateResponse(
        request=request,
        name="all_items.html",
        context={
            "request": request,
            "active_nav": "runs" if status == "ai_failed" else "all",
            "items": items,
            "filters": filters,
            "filter_options": filter_options,
            "page": page,
            "has_next_page": len(items) == 50,
        },
    )


def _parse_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "keep"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "drop"}:
        return False
    return None
