from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from dateutil import parser as date_parser

from app.config.settings import DEFAULT_USER_AGENT
from app.config.source_registry import SourceConfig
from app.parsers.feed_parser import ParsedFeedItem


class GitHubAPICollector:
    """Fetch GitHub REST API JSON and adapt it to ParsedFeedItem records."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        token: str | None = None,
        api_version: str = "2022-11-28",
        timeout_seconds: float = 20.0,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.transport = transport

    def collect(self, source: SourceConfig, limit: int | None = None) -> list[ParsedFeedItem]:
        effective_limit = limit if limit is not None else source.default_limit
        if source.source_subtype == "search_repositories":
            return self._collect_repository_search(source, effective_limit)
        if source.source_subtype == "repo_releases":
            return self._collect_repo_releases(source, effective_limit)
        raise ValueError(f"unsupported GitHub source subtype: {source.source_subtype}")

    def _collect_repository_search(self, source: SourceConfig, limit: int) -> list[ParsedFeedItem]:
        if not source.search_query:
            raise ValueError(f"GitHub repository search source {source.id} requires search_query")

        payload = self._get_json(
            source.url,
            params={
                "q": source.search_query,
                "sort": "updated",
                "order": "desc",
                "per_page": str(_per_page(limit)),
            },
        )
        if payload is None:
            return []
        if not isinstance(payload, dict):
            raise ValueError("GitHub repository search response must be a JSON object")

        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("GitHub repository search response items must be a list")
        return [_repo_to_item(source.id, repo) for repo in raw_items[:limit] if isinstance(repo, dict)]

    def _collect_repo_releases(self, source: SourceConfig, limit: int) -> list[ParsedFeedItem]:
        payload = self._get_json(source.url, params={"per_page": str(_per_page(limit))})
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise ValueError("GitHub releases response must be a JSON array")

        owner, repo = _owner_repo_from_releases_url(source.url)
        return [
            _release_to_item(source.id, release, owner=owner, repo=repo)
            for release in payload[:limit]
            if isinstance(release, dict)
        ]

    def _get_json(self, url: str, *, params: dict[str, str]) -> Any | None:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": self.api_version,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request_url = _absolute_api_url(url, self.base_url)
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers=headers,
            transport=self.transport,
        ) as client:
            response = client.get(request_url, params=params)
            if response.status_code == 304:
                return None
            response.raise_for_status()
            return response.json()


def _repo_to_item(source_id: str, repo: dict[str, Any]) -> ParsedFeedItem:
    full_name = _text(repo.get("full_name")) or _text(repo.get("name")) or "unknown/repository"
    link = _text(repo.get("html_url"))
    description = _text(repo.get("description"))
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    author = _text(owner.get("login")) if isinstance(owner, dict) else None
    details = {
        "description": description,
        "stars": repo.get("stargazers_count"),
        "forks": repo.get("forks_count"),
        "language": repo.get("language"),
        "topics": repo.get("topics"),
        "created_at": repo.get("created_at"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "homepage": repo.get("homepage"),
    }
    raw_content = json.dumps(details, ensure_ascii=False, default=str)
    title = f"GitHub repo: {full_name}"
    return ParsedFeedItem(
        source_id=source_id,
        external_id=f"github_repo:{repo.get('id') or full_name}",
        title=title,
        link=link,
        author=author,
        published_at=_parse_datetime(repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at")),
        raw_summary=description,
        raw_content=raw_content,
        raw_payload={"github_item_type": "repository", **repo},
        content_hash=_content_hash(title=title, link=link, summary=description, content=raw_content),
    )


def _release_to_item(source_id: str, release: dict[str, Any], *, owner: str | None, repo: str | None) -> ParsedFeedItem:
    repo_name = f"{owner}/{repo}" if owner and repo else "GitHub repository"
    tag_name = _text(release.get("tag_name"))
    release_name = _text(release.get("name"))
    title_suffix = release_name or tag_name or str(release.get("id") or "release")
    title = f"{repo_name} release: {title_suffix}"
    link = _text(release.get("html_url"))
    author_data = release.get("author") if isinstance(release.get("author"), dict) else {}
    author = _text(author_data.get("login")) if isinstance(author_data, dict) else None
    body = _text(release.get("body"))
    return ParsedFeedItem(
        source_id=source_id,
        external_id=f"github_release:{release.get('id') or tag_name or link}",
        title=title,
        link=link,
        author=author,
        published_at=_parse_datetime(release.get("published_at") or release.get("created_at")),
        raw_summary=body,
        raw_content=body,
        raw_payload={"github_item_type": "release", **release},
        content_hash=_content_hash(title=title, link=link, summary=body, content=body),
    )


def _absolute_api_url(url: str, base_url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url
    return f"{base_url}/{url.lstrip('/')}"


def _owner_repo_from_releases_url(url: str) -> tuple[str | None, str | None]:
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    if len(path_parts) >= 4 and path_parts[0] == "repos" and path_parts[3] == "releases":
        return path_parts[1], path_parts[2]
    return None, None


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    parsed = date_parser.parse(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _content_hash(*, title: str, link: str | None, summary: str | None, content: str | None) -> str:
    canonical = "\n".join([title.strip().lower(), link or "", summary or "", content or ""])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _per_page(limit: int) -> int:
    return max(1, min(limit, 100))
