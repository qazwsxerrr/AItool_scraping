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
    source_group: str | None = None,
    run_date: str | None = None,
    repo: UIReadRepository = Depends(get_repository),
    github_reader: GitHubProjectReader = Depends(get_github_project_reader),
):
    active_snapshot = repo.resolve_snapshot(edition_date=run_date)
    stats = repo.get_dashboard_stats(snapshot=active_snapshot)
    cards = repo.list_featured_cards(
        category=category,
        source_group=source_group,
        snapshot=active_snapshot,
        limit=30,
    )
    github_projects = github_reader.list_projects(limit=5)
    grouped_cards: list[dict[str, object]] = []
    buckets: dict[str, list[object]] = {}
    for card in cards:
        key = card.topic_category or card.content_class or "未分类"
        buckets.setdefault(key, []).append(card)
    for key, rows in buckets.items():
        grouped_cards.append({"name": key, "cards": rows})
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "request": request,
            "active_nav": "home",
            "stats": stats,
            "cards": cards,
            "grouped_cards": grouped_cards,
            "category": category,
            "source_group": source_group,
            "active_snapshot": active_snapshot,
            "active_edition_date": active_snapshot.edition_date if active_snapshot else None,
            "edition_options": repo.list_daily_editions(),
            "category_options": stats.category_counts,
            "source_options": stats.source_counts,
            "github_projects": github_projects.rows,
            "github_project_stats": github_projects.stats,
        },
    )
