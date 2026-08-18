from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.storage.github_reader import GitHubProjectReader
from app.storage.read_repository import SearchContentResults, UIReadRepository
from app.web.deps import get_github_project_reader, get_repository, templates


router = APIRouter()


@router.get("/search")
def search(
    request: Request,
    q: str | None = None,
    run_date: str | None = None,
    repo: UIReadRepository = Depends(get_repository),
    github_reader: GitHubProjectReader = Depends(get_github_project_reader),
):
    query = (q or "").strip()
    active_snapshot = repo.resolve_snapshot(edition_date=run_date)
    results = repo.search_content(query, snapshot=active_snapshot) if query else SearchContentResults.empty()
    github_projects = github_reader.search(query) if query else []
    return templates.TemplateResponse(
        request=request,
        name="search.html",
        context={
            "request": request,
            "active_nav": "search",
            "query": query,
            "results": results,
            "github_projects": github_projects,
            "total_count": results.total_count + len(github_projects),
            "active_snapshot": active_snapshot,
            "active_edition_date": active_snapshot.edition_date if active_snapshot else None,
            "edition_options": repo.list_daily_editions(),
        },
    )
