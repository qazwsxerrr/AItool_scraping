from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.github.report_reader import GitHubHotspotReportReader
from app.storage.read_repository import UIReadRepository
from app.web.deps import get_github_hotspot_reader, get_repository, templates


router = APIRouter()


@router.get("/")
def index(
    request: Request,
    category: str | None = None,
    direct: bool = False,
    hide_stale: bool = True,
    repo: UIReadRepository = Depends(get_repository),
    github_reader: GitHubHotspotReportReader = Depends(get_github_hotspot_reader),
):
    stats = repo.get_dashboard_stats()
    cards = repo.list_featured_cards(
        category=category,
        direct_support_only=direct,
        hide_stale=hide_stale,
        limit=30,
    )
    github_hotspots = github_reader.list_hotspots(limit=5)
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "request": request,
            "active_nav": "home",
            "stats": stats,
            "cards": cards,
            "top_card": cards[0] if cards else None,
            "category": category,
            "direct": direct,
            "hide_stale": hide_stale,
            "github_hotspots": github_hotspots.rows,
            "github_hotspot_stats": github_hotspots.stats,
        },
    )
