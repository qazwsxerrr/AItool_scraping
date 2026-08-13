from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.storage.github_reader import GitHubProjectReader
from app.storage.read_repository import UIReadRepository
from app.web.deps import get_github_project_reader, get_repository, templates


router = APIRouter()


@router.get("/")
def index(
    request: Request,
    category: str | None = None,
    repo: UIReadRepository = Depends(get_repository),
    github_reader: GitHubProjectReader = Depends(get_github_project_reader),
):
    stats = repo.get_dashboard_stats()
    cards = repo.list_featured_cards(
        category=category,
        limit=30,
    )
    github_projects = github_reader.list_projects(limit=5)
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
            "github_projects": github_projects.rows,
            "github_project_stats": github_projects.stats,
        },
    )
