from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.storage.github_reader import GitHubProjectFilters, GitHubProjectReader
from app.web.deps import get_github_project_reader, templates


router = APIRouter()


@router.get("/github")
def github_projects_page(
    request: Request,
    q: str | None = None,
    language: str | None = None,
    min_stars: int | None = Query(default=None, ge=0),
    min_forks: int | None = Query(default=None, ge=0),
    include_archived: str | None = Query(default="1"),
    reader: GitHubProjectReader = Depends(get_github_project_reader),
):
    filters = GitHubProjectFilters(
        query=(q or None),
        language=(language or None),
        min_stars=min_stars,
        min_forks=min_forks,
        include_archived=_parse_bool(include_archived, default=True),
    )
    all_projects = reader.list_projects()
    result = reader.list_projects(filters=filters, limit=100)
    languages = sorted({row.primary_language for row in all_projects.rows if row.primary_language})

    return templates.TemplateResponse(
        request=request,
        name="github.html",
        context={
            "request": request,
            "active_nav": "github",
            "filters": filters,
            "rows": result.rows,
            "stats": result.stats,
            "all_stats": all_projects.stats,
            "languages": languages,
        },
    )


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
