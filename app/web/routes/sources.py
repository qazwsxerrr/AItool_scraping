from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.storage.read_repository import UIReadRepository
from app.web.deps import get_repository, templates


router = APIRouter()


@router.get("/sources")
def sources_page(
    request: Request,
    repo: UIReadRepository = Depends(get_repository),
):
    rows = repo.list_sources()
    return templates.TemplateResponse(
        request=request,
        name="sources.html",
        context={
            "request": request,
            "active_nav": "sources",
            "sources": rows,
            "edition_options": repo.list_daily_editions(),
        },
    )
