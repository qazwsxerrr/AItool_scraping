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
    edition_date: str | None = None,
    repo: UIReadRepository = Depends(get_repository),
    github_reader: GitHubProjectReader = Depends(get_github_project_reader),
):
    active_edition = repo.resolve_edition(edition_date=edition_date)
    stats = repo.get_dashboard_stats(edition=active_edition)
    all_editions = repo.list_daily_editions()
    
    edition_groups: list[dict[str, object]] = []
    for ed in all_editions[:7]:
        ed_cards = repo.list_featured_cards(
            category=category,
            source_group=source_group,
            edition=ed,
            limit=30,
        )
        is_open = (active_edition is not None and ed.edition_date == active_edition.edition_date)
        edition_groups.append({
            "edition": ed,
            "cards": ed_cards,
            "is_open": is_open,
        })

    active_cards = repo.list_featured_cards(
        category=category,
        source_group=source_group,
        edition=active_edition,
        limit=30,
    )
    github_projects = github_reader.list_projects(limit=5)
    grouped_cards: list[dict[str, object]] = []
    buckets: dict[str, list[object]] = {}
    for card in active_cards:
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
            "cards": active_cards,
            "grouped_cards": grouped_cards,
            "edition_groups": edition_groups,
            "category": category,
            "source_group": source_group,
            "active_edition": active_edition,
            "active_edition_date": active_edition.edition_date if active_edition else None,
            "edition_options": all_editions,
            "category_options": stats.category_counts,
            "source_options": stats.source_counts,
            "github_projects": github_projects.rows,
            "github_project_stats": github_projects.stats,
        },
    )
