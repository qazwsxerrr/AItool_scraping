"""GitHub Search, Releases, Trending, and bounded enrichment collectors."""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.config.settings import DEFAULT_USER_AGENT
from app.domain.models import FetchBatch, FetchItem, SourceSpec
from .base import Collector
from .common import canonical_github_repo_url, number, owner_repo, parse_dt, text
from .http import (
    HTTPClient,
    absolute_url,
    failed_batch,
    request_with_retry,
    response_bytes,
    response_final_url,
    response_request_url,
)

GITHUB_METADATA_MAX_RESPONSE_BYTES = 512 * 1024
GITHUB_README_MAX_RESPONSE_BYTES = 1 * 1024 * 1024
GITHUB_README_MAX_CHARS = 16_000

class GitHubCollector(Collector):
    """GitHub REST API collector for repository search and releases."""

    def __init__(
        self,
        client: HTTPClient,
        *,
        base_url: str = "https://api.github.com",
        token: str | None = None,
        api_version: str = "2022-11-28",
        user_agent: str = DEFAULT_USER_AGENT,
        retries: int = 2,
        timeout_seconds: float | None = None,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.api_version = api_version
        self.user_agent = user_agent
        self.retries = max(0, int(retries))
        self.timeout_seconds = timeout_seconds

    def collect(
        self,
        source: SourceSpec,
        limit: int,
        request_headers: Mapping[str, str] | None = None,
    ) -> FetchBatch:
        if source.transport != "github" or source.github is None or source.github.mode not in {"search", "releases"}:
            return failed_batch(source, "invalid_source", "GitHub REST collector requires github.mode=search or releases")
        params: dict[str, str] = {"per_page": str(max(1, min(int(limit), 100)))}
        options = source.github
        mode = options.mode
        if mode == "search":
            query = _build_search_query(options.query, options.pushed_days)
            if not query:
                return failed_batch(source, "missing_query", "GitHub search mode requires github.query")
            params.update({"q": query, "sort": options.sort, "order": options.order})
        elif mode != "releases":
            return failed_batch(source, "invalid_mode", f"GitHub REST collector does not support mode={mode}")
        response, retry_count, error = request_with_retry(
            self.client,
            absolute_url(source.url, self.base_url),
            retries=self.retries,
            user_agent=self.user_agent,
            extra_headers={
                **dict(request_headers or {}),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.api_version,
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            params=params,
            timeout_seconds=self.timeout_seconds,
        )
        if error is not None:
            return failed_batch(
                source,
                error.code,
                error.message,
                http_status=error.status,
                response_bytes=getattr(error, "response_bytes", 0),
                retry_count=retry_count,
                request_url=source.url,
            )
        assert response is not None
        status_code = int(getattr(response, "status_code", 0) or 0)
        final_url = response_final_url(response, source.url)
        request_url = response_request_url(response, source.url)
        if status_code == 304:
            return FetchBatch(source=source, status="not_modified", http_status=304, request_url=request_url, final_url=final_url, transport="github_api", retry_count=retry_count)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            return failed_batch(
                source,
                "invalid_json",
                str(exc),
                http_status=status_code,
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                response_bytes=response_bytes(response),
            )
        if mode == "releases":
            if not isinstance(payload, list):
                return failed_batch(
                    source,
                    "invalid_payload",
                    "GitHub releases response must be a list",
                    http_status=status_code,
                    retry_count=retry_count,
                    request_url=request_url,
                    final_url=final_url,
                    response_bytes=response_bytes(response),
                )
            items = [_release_to_item(source, value) for value in payload[:limit] if isinstance(value, dict)]
        else:
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                return failed_batch(
                    source,
                    "invalid_payload",
                    "GitHub search response must contain items",
                    http_status=status_code,
                    retry_count=retry_count,
                    request_url=request_url,
                    final_url=final_url,
                    response_bytes=response_bytes(response),
                )
            items = [_repo_to_item(source, value, query=params.get("q")) for value in payload["items"][:limit] if isinstance(value, dict)]
        return FetchBatch(
            source=source,
            items=items,
            status="success",
            http_status=status_code,
            request_url=request_url,
            final_url=final_url,
            response_bytes=len(getattr(response, "content", b"") or b""),
            retry_count=retry_count,
            transport="github_api",
        )

    def lookup_repository(self, owner: str, repo: str) -> dict[str, Any] | None:
        """Fetch one repository metadata object for a Product Hunt link."""

        url = f"{self.base_url}/repos/{owner}/{repo}"
        response, _, error = request_with_retry(
            self.client,
            url,
            retries=self.retries,
            user_agent=self.user_agent,
            extra_headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.api_version,
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            max_response_bytes=GITHUB_METADATA_MAX_RESPONSE_BYTES,
            timeout_seconds=self.timeout_seconds,
        )
        if error is not None or response is None:
            return None
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return None
        return dict(payload) if isinstance(payload, dict) else None

    def enrich_repository(
        self,
        owner: str,
        repo: str,
        *,
        max_readme_chars: int = GITHUB_README_MAX_CHARS,
    ) -> dict[str, Any]:
        """Fetch bounded metadata and README text for one selected repository.

        This is a metadata enrichment boundary: a 404, rate-limit, or
        malformed README is returned as telemetry and never raises to the
        batch.  The caller can persist the partial result and retain the
        deterministic ``hotspot`` status.
        """

        owner_text = str(owner or "").strip()
        repo_text = str(repo or "").strip().removesuffix(".git")
        result: dict[str, Any] = {
            "owner": owner_text,
            "repo": repo_text,
            "metadata": {},
            "readme_text": None,
            "readme_present": None,
            "readme_checked": False,
            "errors": [],
        }
        if not owner_text or not repo_text:
            result["errors"].append("invalid_repository_identity")
            return result

        headers = self._github_headers()
        metadata_url = f"{self.base_url}/repos/{owner_text}/{repo_text}"
        response, retry_count, error = request_with_retry(
            self.client,
            metadata_url,
            retries=self.retries,
            user_agent=self.user_agent,
            extra_headers=headers,
            max_response_bytes=GITHUB_METADATA_MAX_RESPONSE_BYTES,
            timeout_seconds=self.timeout_seconds,
        )
        result["metadata_retry_count"] = retry_count
        if error is not None or response is None:
            result["errors"].append(f"metadata:{error.code if error else 'request_error'}")
        else:
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                result["metadata"] = dict(payload)
            else:
                result["errors"].append("metadata:invalid_json")

        readme_url = f"{self.base_url}/repos/{owner_text}/{repo_text}/readme"
        response, retry_count, error = request_with_retry(
            self.client,
            readme_url,
            retries=self.retries,
            user_agent=self.user_agent,
            extra_headers={**headers, "Accept": "application/vnd.github.raw+json"},
            max_response_bytes=GITHUB_README_MAX_RESPONSE_BYTES,
            timeout_seconds=self.timeout_seconds,
        )
        result["readme_retry_count"] = retry_count
        result["readme_checked"] = True
        if error is not None or response is None:
            result["readme_present"] = False if error and error.status == 404 else None
            result["errors"].append(f"readme:{error.code if error else 'request_error'}")
            return result

        text = _decode_github_readme_response(response, max_chars=max_readme_chars)
        result["readme_text"] = text
        result["readme_present"] = bool(text)
        if text is None:
            result["errors"].append("readme:invalid_payload")
        return result

    def _github_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        }


class GitHubTrendingCollector(Collector):
    """Fetch GitHub's daily/weekly Trending pages directly as HTML."""

    def __init__(
        self,
        client: HTTPClient,
        *,
        retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
        max_response_bytes: int = 4 * 1024 * 1024,
        sleeper=time.sleep,
    ) -> None:
        self.client = client
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.max_response_bytes = max_response_bytes
        self.sleeper = sleeper

    def collect(
        self,
        source: SourceSpec,
        limit: int,
        request_headers: Mapping[str, str] | None = None,
    ) -> FetchBatch:
        if source.transport != "github" or source.github is None or source.github.mode != "trending":
            return failed_batch(source, "invalid_source", "GitHub Trending collector requires github.mode=trending")
        response, retry_count, error = request_with_retry(
            self.client,
            source.url,
            retries=self.retries,
            user_agent=self.user_agent,
            max_response_bytes=self.max_response_bytes,
            extra_headers={
                **dict(request_headers or {}),
                "Accept": "text/html,application/xhtml+xml",
            },
            sleeper=self.sleeper,
        )
        if error is not None:
            return failed_batch(
                source,
                error.code,
                error.message,
                http_status=error.status,
                response_bytes=getattr(error, "response_bytes", 0),
                retry_count=retry_count,
                request_url=source.url,
                transport="github_trending_html",
            )
        assert response is not None
        request_url = response_request_url(response, source.url)
        final_url = response_final_url(response, source.url)
        body = bytes(getattr(response, "content", b"") or b"")
        try:
            html = body.decode("utf-8", errors="replace")
            repos = _parse_github_trending_html(
                html,
                period=_trending_period(source),
                limit=max(1, int(limit)),
                source_id=source.id,
            )
        except (TypeError, ValueError) as exc:
            return failed_batch(
                source,
                "invalid_html",
                str(exc),
                http_status=int(getattr(response, "status_code", 0) or 0),
                response_bytes=len(body),
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                transport="github_trending_html",
            )
        if not repos:
            soup = BeautifulSoup(html, "html.parser")
            if soup.select_one(".blankslate"):
                return FetchBatch(
                    source=source,
                    items=[],
                    status="success",
                    http_status=int(getattr(response, "status_code", 0) or 0),
                    request_url=request_url,
                    final_url=final_url,
                    response_bytes=len(body),
                    retry_count=retry_count,
                    transport="github_trending_html",
                )
            return failed_batch(
                source,
                "trending_parse_empty",
                "GitHub Trending HTML contained no repository rows",
                http_status=int(getattr(response, "status_code", 0) or 0),
                response_bytes=len(body),
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                transport="github_trending_html",
            )
        return FetchBatch(
            source=source,
            items=repos,
            status="success",
            http_status=int(getattr(response, "status_code", 0) or 0),
            request_url=request_url,
            final_url=final_url,
            response_bytes=len(body),
            retry_count=retry_count,
            transport="github_trending_html",
        )


def _repo_to_item(source: SourceSpec, repo: dict[str, Any], *, query: str | None) -> FetchItem:
    full_name = text(repo.get("full_name") or repo.get("name")) or "unknown/repository"
    url = text(repo.get("html_url"))
    canonical_url = canonical_github_repo_url(full_name) or url
    description = text(repo.get("description"))
    topics = repo.get("topics") if isinstance(repo.get("topics"), list) else []
    metrics = {
        "stars": number(repo.get("stargazers_count")),
        "forks": number(repo.get("forks_count")),
        "language": repo.get("language"),
        "topics": topics,
        "full_name": full_name,
        "canonical_project_key": full_name,
        "github_owner": full_name.split("/", 1)[0] if "/" in full_name else None,
        "github_repo": full_name.split("/", 1)[1] if "/" in full_name else full_name,
        "description": description,
        "created_at": repo.get("created_at"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "archived": bool(repo.get("archived", False)),
        "fork": bool(repo.get("fork", False)),
        "license": (repo.get("license") or {}).get("spdx_id") if isinstance(repo.get("license"), dict) else None,
        # Repository search does not expose README contents.  Preserve an
        # explicit value when an upstream adapter supplies one, but never use a
        # description as a false README assertion.
        "readme_present": bool(repo["readme_url"]) if "readme_url" in repo else None,
        "has_readme": bool(repo["readme_url"]) if "readme_url" in repo else None,
        "readme_checked": "readme_url" in repo,
        "latest_release": repo.get("latest_release") or repo.get("latest_release_url"),
        "release_url": repo.get("latest_release_url"),
        "query": query,
    }
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    author = text(owner.get("login")) if isinstance(owner, dict) else None
    raw = {"github_item_type": "repository", **repo}
    return FetchItem(
        source_id=source.id,
        # Repository paths are stable across Search, Trending, and product
        # links.  Numeric Search IDs alone would prevent cross-source dedupe.
        external_id=f"github_repo:{full_name.casefold()}",
        title=f"GitHub repo: {full_name}",
        url=url,
        canonical_url=canonical_url,
        author=author,
        published_at=parse_dt(repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at")),
        summary=description,
        content=json.dumps(metrics, ensure_ascii=False, default=str),
        metrics=metrics,
        raw_payload=raw,
        kind="github_repository",
    )


def _release_to_item(source: SourceSpec, release: dict[str, Any]) -> FetchItem:
    owner, repo = owner_repo(source.url or "")
    repo_name = f"{owner}/{repo}" if owner and repo else "GitHub repository"
    name = text(release.get("name") or release.get("tag_name") or release.get("id")) or "release"
    body = text(release.get("body"))
    author_data = release.get("author") if isinstance(release.get("author"), dict) else {}
    return FetchItem(
        source_id=source.id,
        external_id=f"github_release:{release.get('id') or release.get('tag_name') or release.get('html_url')}",
        title=f"{repo_name} release: {name}",
        url=text(release.get("html_url")),
        author=text(author_data.get("login")) if isinstance(author_data, dict) else None,
        published_at=parse_dt(release.get("published_at") or release.get("created_at")),
        summary=body,
        content=body,
        content_depth="full" if body else "missing",
        metrics={"tag_name": release.get("tag_name"), "draft": bool(release.get("draft")), "prerelease": bool(release.get("prerelease"))},
        raw_payload={"github_item_type": "release", **release},
        kind="github_release",
    )


def _parse_github_trending_html(
    html: str,
    *,
    period: str,
    limit: int,
    source_id: str,
) -> list[FetchItem]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("article.Box-row")
    items: list[FetchItem] = []
    captured_at = datetime.now(timezone.utc)
    for rank, row in enumerate(rows[:limit], start=1):
        link = row.select_one("h2 a, h3 a")
        href = text(link.get("href") if link else None)
        full_name = _github_trending_full_name(href)
        if not full_name:
            continue

        description_node = row.select_one("p")
        language_node = row.select_one("[itemprop='programmingLanguage']")
        stars_node = _find_github_trending_link(row, "stargazers")
        forks_node = _find_github_trending_link(row, "forks")
        stars = number(stars_node.get_text(" ", strip=True) if stars_node else None) or 0
        forks = number(forks_node.get_text(" ", strip=True) if forks_node else None) or 0
        stars_since = _github_trending_stars_since(row, period)
        owner = full_name.split("/", 1)[0]
        metrics = {
            "stars": stars,
            "forks": forks,
            "stars_since": stars_since,
            "trending_period": period,
            "trending_rank": rank,
            "trending_signal": "stars_since",
            "discovery_sources": [source_id],
            "full_name": full_name,
            "canonical_project_key": full_name,
            "github_owner": owner,
            "github_repo": full_name.split("/", 1)[1],
            "language": text(language_node.get_text(" ", strip=True) if language_node else None),
        }
        url = f"https://github.com/{full_name}"
        canonical_url = canonical_github_repo_url(full_name) or url
        raw_payload = {
            "github_item_type": "repository",
            "full_name": full_name,
            "html_url": url,
            "trending_period": period,
            "trending_rank": rank,
            "stars_since": stars_since,
        }
        items.append(
            FetchItem(
                source_id=source_id,
                content_class="project_tool",
                external_id=f"github_repo:{full_name.casefold()}",
                title=f"GitHub repo: {full_name}",
                url=url,
                canonical_url=canonical_url,
                author=owner,
                published_at=captured_at,
                captured_at=captured_at,
                summary=text(description_node.get_text(" ", strip=True) if description_node else None),
                content=json.dumps(metrics, ensure_ascii=False, default=str),
                metrics=metrics,
                raw_payload=raw_payload,
                kind="github_trending_repository",
            )
        )
    return items


def _github_trending_full_name(href: str | None) -> str | None:
    if not href:
        return None
    parts = [part for part in urlparse(href).path.split("/") if part]
    if len(parts) != 2 or any(part in {"trending", "sponsors", "collections"} for part in parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def _decode_github_readme_response(response: Any, *, max_chars: int) -> str | None:
    """Decode either the GitHub JSON README envelope or a raw text response."""

    try:
        payload = response.json()
    except (TypeError, ValueError, AttributeError):
        payload = None
    if isinstance(payload, dict):
        content = payload.get("content")
        encoding = str(payload.get("encoding") or "").casefold()
        if isinstance(content, str) and encoding == "base64":
            try:
                content = base64.b64decode(content.encode("ascii"), validate=False).decode("utf-8", errors="replace")
            except (ValueError, UnicodeError):
                return None
        if isinstance(content, str):
            return content[: max(0, int(max_chars))]
        return None
    raw = getattr(response, "content", b"")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")[: max(0, int(max_chars))] or None
    if isinstance(raw, str):
        return raw[: max(0, int(max_chars))] or None
    return None


def _find_github_trending_link(row: Any, fragment: str) -> Any | None:
    for link in row.select("a[href]"):
        href = str(link.get("href") or "")
        if f"/{fragment}" in href:
            return link
    return None


def _github_trending_stars_since(row: Any, period: str) -> int:
    text = row.get_text(" ", strip=True)
    period_pattern = {
        "daily": r"([0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)\s+stars?\s+today",
        "weekly": r"([0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)\s+stars?\s+this\s+week",
        "monthly": r"([0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)\s+stars?\s+this\s+month",
    }
    match = re.search(period_pattern.get(period, period_pattern["daily"]), text, flags=re.IGNORECASE)
    if match:
        return int(number(match.group(1)) or 0)
    for node in row.select(".float-sm-right"):
        value = number(node.get_text(" ", strip=True))
        if value is not None:
            return int(value)
    return 0


def _trending_period(source: SourceSpec) -> str:
    options = source.github
    if options is None or options.mode != "trending":
        return "daily"
    return options.period or "daily"


def _build_search_query(query: str | None, pushed_days: int | None) -> str:
    if not query:
        return ""
    normalized = query.strip()
    if pushed_days and not re.search(r"\bpushed:", normalized, flags=re.IGNORECASE):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=pushed_days)).date().isoformat()
        normalized = f"{normalized} pushed:>{cutoff}"
    return normalized

__all__ = ["GitHubCollector", "GitHubTrendingCollector"]
