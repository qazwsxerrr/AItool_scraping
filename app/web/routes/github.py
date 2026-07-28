from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.github.report_reader import GitHubHotspotFilters, GitHubHotspotReportReader
from app.web.deps import get_github_hotspot_reader, templates


router = APIRouter()


@router.get("/github")
def github_hotspots(
    request: Request,
    q: str | None = None,
    level: str | None = None,
    language: str | None = None,
    project_type: str | None = None,
    min_score: int | None = Query(default=None, ge=0, le=100),
    include_risky: str | None = Query(default="1"),
    reader: GitHubHotspotReportReader = Depends(get_github_hotspot_reader),
):
    filters = GitHubHotspotFilters(
        query=(q or None),
        level=(level or None),
        language=(language or None),
        project_type=(project_type or None),
        min_score=min_score,
        include_risky=_parse_bool(include_risky, default=True),
    )
    all_hotspots = reader.list_hotspots()
    result = reader.list_hotspots(filters=filters, limit=100)
    languages = sorted({row.primary_language for row in all_hotspots.rows if row.primary_language})
    project_types = sorted({row.project_type for row in all_hotspots.rows if row.project_type})

    return templates.TemplateResponse(
        request=request,
        name="github.html",
        context={
            "request": request,
            "active_nav": "github",
            "filters": filters,
            "rows": result.rows,
            "stats": result.stats,
            "all_stats": all_hotspots.stats,
            "languages": languages,
            "project_types": project_types,
        },
    )


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
