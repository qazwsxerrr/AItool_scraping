"""Shared-client collectors for the simplified intelligence pipeline.

Collectors only perform transport and field mapping. They return a
``FetchBatch`` and never touch SQLAlchemy, scoring, or AI code.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

import feedparser
import httpx
from dateutil import parser as date_parser

from app.config.settings import DEFAULT_USER_AGENT
from app.domain.models import FetchBatch, FetchItem, SourceSpec
from app.parsers.feed_parser import parse_feed


class HTTPClient(Protocol):
    def get(self, url: str, **kwargs: Any): ...

    def post(self, url: str, **kwargs: Any): ...


_PRODUCTHUNT_POSTS_QUERY = """
query IntelligencePosts($first: Int!, $postedAfter: DateTime!) {
  posts(first: $first, postedAfter: $postedAfter, order: VOTES) {
    nodes {
      id
      name
      tagline
      description
      url
      website
      votesCount
      commentsCount
      createdAt
      featuredAt
      dailyRank
      weeklyRank
      productLinks { type url }
    }
  }
}
""".strip()


@dataclass(frozen=True)
class _RequestFailure:
    """Failure telemetry with tuple-style access for the collector boundary."""

    code: str
    message: str
    status: int | None
    response_bytes: int = 0

    def __iter__(self):
        yield self.code
        yield self.message
        yield self.status

    def __getitem__(self, index: int):
        return (self.code, self.message, self.status)[index]


class Collector:
    def collect(self, source: SourceSpec, limit: int) -> FetchBatch:
        raise NotImplementedError


class FeedCollector(Collector):
    """RSS/Atom/RSSHub collector using the caller-provided shared client."""

    def __init__(
        self,
        client: HTTPClient,
        *,
        retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
        max_response_bytes: int = 2 * 1024 * 1024,
        sleeper=time.sleep,
    ) -> None:
        self.client = client
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.max_response_bytes = max_response_bytes
        self.sleeper = sleeper

    def collect(self, source: SourceSpec, limit: int) -> FetchBatch:
        if not source.url:
            return _failed_batch(source, "missing_url", "source has no URL")
        response, retry_count, error = _request_with_retry(
            self.client,
            source.url,
            retries=self.retries,
            user_agent=self.user_agent,
            max_response_bytes=self.max_response_bytes,
            sleeper=self.sleeper,
        )
        if error is not None:
            return _failed_batch(
                source,
                error[0],
                error[1],
                http_status=error[2],
                response_bytes=getattr(error, "response_bytes", 0),
                retry_count=retry_count,
                request_url=source.url,
            )
        assert response is not None
        final_url = _response_final_url(response, source.url)
        request_url = _response_request_url(response, source.url)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code == 304:
            return FetchBatch(
                source=source,
                items=[],
                status="not_modified",
                http_status=status_code,
                request_url=request_url,
                final_url=final_url,
                response_bytes=0,
                retry_count=retry_count,
                transport="httpx",
            )
        body = bytes(getattr(response, "content", b""))
        try:
            parsed = parse_feed(body, source_id=source.id)
        except Exception as exc:
            return _failed_batch(
                source,
                "parse_error",
                str(exc),
                http_status=status_code,
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                response_bytes=len(body),
            )
        items = [_feed_item_to_domain(item, source) for item in parsed[: max(0, limit)]]
        return FetchBatch(
            source=source,
            items=items,
            status="success",
            http_status=status_code,
            request_url=request_url,
            final_url=final_url,
            response_bytes=len(body),
            retry_count=retry_count,
            transport="httpx",
        )


class RSSCollector(FeedCollector):
    pass


class RSSHubCollector(FeedCollector):
    pass


class ProductHuntCollector(FeedCollector):
    """Collect Product Hunt posts from the API when configured, else Atom."""

    def __init__(
        self,
        *args: Any,
        github_lookup=None,
        api_token: str | None = None,
        api_url: str = "https://api.producthunt.com/v2/api/graphql",
        timeout_seconds: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.github_lookup = github_lookup
        self.api_token = api_token
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def collect(self, source: SourceSpec, limit: int) -> FetchBatch:
        batch = self._collect_api(source, limit) if self.api_token else super().collect(source, limit)
        for item in batch.items:
            payload = dict(item.raw_payload)
            metrics = dict(item.metrics)
            _copy_metric(
                metrics,
                payload,
                "votes",
                ("votes", "voteCount", "vote_count", "points", "upvotes", "ph_vote_count", "ph_votes"),
            )
            _copy_metric(
                metrics,
                payload,
                "comments",
                ("comments", "commentCount", "comment_count", "comments_count", "ph_comments_count", "ph_comment_count"),
            )
            _extract_feed_engagement(metrics, item)
            if "votes" not in metrics and "comments" not in metrics:
                # The public Atom feed currently omits engagement metrics. Keep
                # the degradation explicit instead of treating zero as a real
                # vote count.
                metrics["producthunt_metrics_status"] = "unavailable_in_feed"
            else:
                metrics["producthunt_metrics_status"] = "available"
            github_url = _find_github_url(
                item.url,
                item.content,
                item.summary,
                payload.get("website"),
                json.dumps(payload.get("productLinks") or payload.get("product_links") or [], default=str),
            )
            if github_url:
                metrics.setdefault("github_url", github_url)
                owner, repo = _github_owner_repo_from_html_url(github_url)
                if owner and repo:
                    metrics.setdefault("github_owner", owner)
                    metrics.setdefault("github_repo", repo)
                    metrics.setdefault("canonical_project_key", f"{owner}/{repo}")
                    # Use the canonical repository identity for cross-source
                    # idempotency (GitHub Search and Product Hunt can mention
                    # the same project).
                    item.external_id = f"github_repo:{owner.casefold()}/{repo.casefold()}"
                    if self.github_lookup is not None:
                        try:
                            github_payload = self.github_lookup(owner, repo)
                            if isinstance(github_payload, dict):
                                _merge_github_metrics(metrics, github_payload)
                                if github_payload.get("id") is not None:
                                    item.external_id = f"github_repo:{github_payload['id']}"
                        except Exception as exc:
                            metrics.setdefault("github_enrichment_error", type(exc).__name__)
            item.metrics = metrics
            item.raw_payload = payload
        return batch

    def _collect_api(self, source: SourceSpec, limit: int) -> FetchBatch:
        cutoff_days = (
            source.selection_policy.max_age_days
            or source.selection_policy.time_window_days
            or 30
        )
        response, retry_count, error = _request_with_retry(
            self.client,
            self.api_url,
            method="post",
            json_body={
                "query": _PRODUCTHUNT_POSTS_QUERY,
                "variables": {
                    "first": max(1, min(int(limit), 100)),
                    "postedAfter": (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).isoformat(),
                },
            },
            retries=self.retries,
            user_agent=self.user_agent,
            max_response_bytes=self.max_response_bytes,
            extra_headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_token}",
            },
            timeout_seconds=self.timeout_seconds,
            sleeper=self.sleeper,
        )
        if error is not None:
            return _failed_batch(
                source,
                error[0],
                error[1],
                http_status=error[2],
                response_bytes=getattr(error, "response_bytes", 0),
                retry_count=retry_count,
                request_url=self.api_url,
                transport="producthunt_api",
            )
        assert response is not None
        request_url = _response_request_url(response, self.api_url)
        final_url = _response_final_url(response, self.api_url)
        status_code = int(getattr(response, "status_code", 0) or 0)
        response_bytes = _response_bytes(response)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            return _failed_batch(
                source,
                "invalid_json",
                str(exc),
                http_status=status_code,
                response_bytes=response_bytes,
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                transport="producthunt_api",
            )
        if not isinstance(payload, dict) or payload.get("errors"):
            return _failed_batch(
                source,
                "producthunt_api_error",
                _graphql_error_message(payload),
                http_status=status_code,
                response_bytes=response_bytes,
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                transport="producthunt_api",
            )
        data = payload.get("data")
        posts = data.get("posts") if isinstance(data, dict) else None
        nodes = posts.get("nodes") if isinstance(posts, dict) else None
        if not isinstance(nodes, list):
            return _failed_batch(
                source,
                "invalid_payload",
                "Product Hunt response must contain data.posts.nodes",
                http_status=status_code,
                response_bytes=response_bytes,
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                transport="producthunt_api",
            )
        return FetchBatch(
            source=source,
            items=[_producthunt_node_to_item(source, node) for node in nodes[:limit] if isinstance(node, dict)],
            status="success",
            http_status=status_code,
            request_url=request_url,
            final_url=final_url,
            response_bytes=response_bytes,
            retry_count=retry_count,
            transport="producthunt_api",
        )


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

    def collect(self, source: SourceSpec, limit: int) -> FetchBatch:
        if not source.url:
            return _failed_batch(source, "missing_url", "source has no URL")
        params: dict[str, str] = {"per_page": str(max(1, min(int(limit), 100)))}
        if source.source_subtype == "search_repositories":
            query = _build_search_query(source.search_query, source.search_pushed_days)
            if not query:
                return _failed_batch(source, "missing_query", "GitHub search_query is required")
            params.update({"q": query, "sort": source.search_sort, "order": source.search_order})
        response, retry_count, error = _request_with_retry(
            self.client,
            _absolute_url(source.url, self.base_url),
            retries=self.retries,
            user_agent=self.user_agent,
            extra_headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.api_version,
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            params=params,
            timeout_seconds=self.timeout_seconds,
        )
        if error is not None:
            return _failed_batch(
                source,
                error[0],
                error[1],
                http_status=error[2],
                response_bytes=getattr(error, "response_bytes", 0),
                retry_count=retry_count,
                request_url=source.url,
            )
        assert response is not None
        status_code = int(getattr(response, "status_code", 0) or 0)
        final_url = _response_final_url(response, source.url)
        request_url = _response_request_url(response, source.url)
        if status_code == 304:
            return FetchBatch(source=source, status="not_modified", http_status=304, request_url=request_url, final_url=final_url, transport="github_api", retry_count=retry_count)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            return _failed_batch(
                source,
                "invalid_json",
                str(exc),
                http_status=status_code,
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                response_bytes=_response_bytes(response),
            )
        if source.source_subtype == "repo_releases":
            if not isinstance(payload, list):
                return _failed_batch(
                    source,
                    "invalid_payload",
                    "GitHub releases response must be a list",
                    http_status=status_code,
                    retry_count=retry_count,
                    request_url=request_url,
                    final_url=final_url,
                    response_bytes=_response_bytes(response),
                )
            items = [_release_to_item(source, value) for value in payload[:limit] if isinstance(value, dict)]
        else:
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                return _failed_batch(
                    source,
                    "invalid_payload",
                    "GitHub search response must contain items",
                    http_status=status_code,
                    retry_count=retry_count,
                    request_url=request_url,
                    final_url=final_url,
                    response_bytes=_response_bytes(response),
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
        response, _, error = _request_with_retry(
            self.client,
            url,
            retries=self.retries,
            user_agent=self.user_agent,
            extra_headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.api_version,
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            timeout_seconds=self.timeout_seconds,
        )
        if error is not None or response is None:
            return None
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return None
        return dict(payload) if isinstance(payload, dict) else None


class CollectorRouter:
    """Resolve a source to a collector while keeping one shared HTTP client."""

    def __init__(self, *, feed: FeedCollector, rsshub: RSSHubCollector, github: GitHubCollector, producthunt: ProductHuntCollector) -> None:
        self.feed = feed
        self.rsshub = rsshub
        self.github = github
        self.producthunt = producthunt

    def collect(self, source: SourceSpec, limit: int) -> FetchBatch:
        collector_type = source.collector_type or _infer_collector_type(source)
        if collector_type == "github":
            return self.github.collect(source, limit)
        if collector_type == "producthunt":
            return self.producthunt.collect(source, limit)
        if collector_type == "rsshub":
            return self.rsshub.collect(source, limit)
        return self.feed.collect(source, limit)


def _feed_item_to_domain(item: Any, source: SourceSpec) -> FetchItem:
    payload = dict(getattr(item, "raw_payload", {}) or {})
    metrics: dict[str, Any] = {}
    for target, aliases in {
        "stars": ("stars", "stargazers", "stargazers_count", "star_count", "github_stars"),
        "forks": ("forks", "forks_count", "fork_count", "github_forks"),
        "pushed_at": ("pushed_at", "last_push", "updated_at", "github_pushed_at"),
        "votes": ("votes", "vote_count", "upvotes", "points"),
        "comments": ("comments", "comment_count", "comments_count", "num_comments"),
        "views": ("views", "view_count", "impressions"),
        "likes": ("likes", "like_count", "favorite_count"),
        "reposts": ("reposts", "retweets", "retweet_count", "shares"),
        "replies": ("replies", "reply_count", "quote_count"),
        "engagement": ("engagement", "score", "engagement_count"),
    }.items():
        for key in aliases:
            if payload.get(key) is not None:
                metrics[target] = payload[key]
                break
    _extract_feed_project_metrics(metrics, item, source)
    return FetchItem(
        source_id=source.id,
        external_id=getattr(item, "external_id", None),
        title=getattr(item, "title", "(untitled)"),
        url=getattr(item, "link", None),
        author=getattr(item, "author", None),
        published_at=getattr(item, "published_at", None),
        summary=getattr(item, "raw_summary", None),
        content=getattr(item, "raw_content", None),
        metrics=metrics,
        raw_payload=payload,
        kind="feed",
    )


def _extract_feed_project_metrics(metrics: dict[str, Any], item: Any, source: SourceSpec) -> None:
    """Map common GitHub-trending feed text into the canonical project metrics."""

    if source.content_class != "project_tool":
        return
    text = " ".join(
        str(value)
        for value in (
            getattr(item, "title", None),
            getattr(item, "raw_summary", None),
            getattr(item, "raw_content", None),
        )
        if value
    )
    if "stars" not in metrics:
        match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)\s*stars?", text, re.IGNORECASE)
        if match:
            metrics["stars"] = _number(match.group(1))
    if "forks" not in metrics:
        match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?[kKmM]?)\s*forks?", text, re.IGNORECASE)
        if match:
            metrics["forks"] = _number(match.group(1))
    mode = source.selection_policy.mode.casefold().replace("-", "_")
    if mode in {"github_active_high_star", "active_high_star"} and "pushed_at" not in metrics:
        published_at = getattr(item, "published_at", None)
        if published_at is not None:
            metrics["pushed_at"] = published_at.isoformat()


def _producthunt_node_to_item(source: SourceSpec, node: dict[str, Any]) -> FetchItem:
    post_id = _text(node.get("id") or node.get("slug") or node.get("url")) or "unknown"
    title = _text(node.get("name")) or "(untitled)"
    tagline = _text(node.get("tagline"))
    description = _text(node.get("description"))
    product_links = node.get("productLinks") if isinstance(node.get("productLinks"), list) else []
    metrics: dict[str, Any] = {
        "votes": _number(node.get("votesCount")),
        "comments": _number(node.get("commentsCount")),
        "daily_rank": _number(node.get("dailyRank")),
        "weekly_rank": _number(node.get("weeklyRank")),
        "featured_at": node.get("featuredAt"),
        "website": node.get("website"),
        "product_links": product_links,
        "producthunt_metrics_status": "available",
    }
    metrics = {key: value for key, value in metrics.items() if value is not None}
    return FetchItem(
        source_id=source.id,
        external_id=f"producthunt_post:{post_id}",
        title=title,
        url=_text(node.get("url")),
        published_at=_parse_dt(node.get("featuredAt") or node.get("createdAt")),
        summary=tagline or description,
        content=description or tagline,
        metrics=metrics,
        raw_payload={"producthunt_item_type": "post", **node},
        kind="producthunt_post",
    )


def _graphql_error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "Product Hunt API response must be an object"
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return "Product Hunt API returned an error"
    messages = [
        str(error.get("message") or error.get("error_description") or "GraphQL error")
        for error in errors
        if isinstance(error, dict)
    ]
    return "; ".join(messages)[:4000] or "Product Hunt API returned an error"


def _repo_to_item(source: SourceSpec, repo: dict[str, Any], *, query: str | None) -> FetchItem:
    full_name = _text(repo.get("full_name") or repo.get("name")) or "unknown/repository"
    url = _text(repo.get("html_url"))
    description = _text(repo.get("description"))
    metrics = {
        "stars": _number(repo.get("stargazers_count")),
        "forks": _number(repo.get("forks_count")),
        "language": repo.get("language"),
        "topics": repo.get("topics") if isinstance(repo.get("topics"), list) else [],
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
    author = _text(owner.get("login")) if isinstance(owner, dict) else None
    raw = {"github_item_type": "repository", **repo}
    return FetchItem(
        source_id=source.id,
        external_id=f"github_repo:{repo.get('id') or full_name}",
        title=f"GitHub repo: {full_name}",
        url=url,
        author=author,
        published_at=_parse_dt(repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at")),
        summary=description,
        content=json.dumps(metrics, ensure_ascii=False, default=str),
        metrics=metrics,
        raw_payload=raw,
        kind="github_repository",
    )


def _release_to_item(source: SourceSpec, release: dict[str, Any]) -> FetchItem:
    owner, repo = _owner_repo(source.url or "")
    repo_name = f"{owner}/{repo}" if owner and repo else "GitHub repository"
    name = _text(release.get("name") or release.get("tag_name") or release.get("id")) or "release"
    body = _text(release.get("body"))
    author_data = release.get("author") if isinstance(release.get("author"), dict) else {}
    return FetchItem(
        source_id=source.id,
        external_id=f"github_release:{release.get('id') or release.get('tag_name') or release.get('html_url')}",
        title=f"{repo_name} release: {name}",
        url=_text(release.get("html_url")),
        author=_text(author_data.get("login")) if isinstance(author_data, dict) else None,
        published_at=_parse_dt(release.get("published_at") or release.get("created_at")),
        summary=body,
        content=body,
        metrics={"tag_name": release.get("tag_name"), "draft": bool(release.get("draft")), "prerelease": bool(release.get("prerelease"))},
        raw_payload={"github_item_type": "release", **release},
        kind="github_release",
    )


def _request_with_retry(
    client: HTTPClient,
    url: str,
    *,
    method: str = "get",
    json_body: dict[str, Any] | None = None,
    retries: int,
    user_agent: str,
    max_response_bytes: int | None = None,
    extra_headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    sleeper=time.sleep,
) -> tuple[Any | None, int, _RequestFailure | None]:
    headers = {"User-Agent": user_agent, "Accept": "application/rss+xml, application/atom+xml, application/json, */*"}
    headers.update(extra_headers or {})
    last: _RequestFailure | None = None
    last_attempt = 0
    for attempt in range(max(0, retries) + 1):
        last_attempt = attempt
        try:
            request_kwargs: dict[str, Any] = {"headers": headers}
            if params is not None:
                request_kwargs["params"] = params
            if json_body is not None:
                request_kwargs["json"] = json_body
            if timeout_seconds is not None:
                request_kwargs["timeout"] = timeout_seconds
            requester = getattr(client, method.casefold(), None)
            if not callable(requester):
                raise TypeError(f"HTTP client does not support {method.upper()}")
            try:
                response = requester(url, **request_kwargs)
            except TypeError:
                # Tiny injected clients may expose only the minimal request
                # signature, so retry once without the optional timeout.
                request_kwargs.pop("timeout", None)
                response = requester(url, **request_kwargs)
            status = int(getattr(response, "status_code", 0) or 0)
            body = bytes(getattr(response, "content", b"") or b"")
            if max_response_bytes is not None and len(body) > max_response_bytes:
                return None, attempt, _RequestFailure(
                    "response_too_large",
                    f"response exceeds {max_response_bytes} bytes",
                    status,
                    len(body),
                )
            if status == 304:
                return response, attempt, None
            if 200 <= status < 300:
                return response, attempt, None
            code = _http_error_code(status)
            last = _RequestFailure(code, f"HTTP {status}", status, len(body))
            if not _retryable(status, attempt, retries):
                break
            retry_after = _retry_after(response)
            sleeper(retry_after if retry_after is not None else min(0.5 * 2**attempt, 4.0))
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            last = _RequestFailure(
                "timeout" if isinstance(exc, httpx.TimeoutException) else "request_error",
                str(exc),
                None,
                0,
            )
            if attempt >= retries:
                break
            sleeper(min(0.5 * 2**attempt, 4.0))
    return None, last_attempt, last or _RequestFailure("request_error", "request failed", None, 0)


def _failed_batch(source: SourceSpec, code: str, message: str, **kwargs: Any) -> FetchBatch:
    return FetchBatch(source=source, items=[], status="failed", error_code=code, error_message=str(message)[:4000], transport=kwargs.pop("transport", "httpx"), **kwargs)


def _infer_collector_type(source: SourceSpec) -> str:
    if source.type == "github_api":
        return "github"
    if source.source_group == "producthunt" or source.id.startswith("producthunt"):
        return "producthunt"
    if source.type == "rsshub":
        return "rsshub"
    return "rss"


def _absolute_url(url: str, base_url: str) -> str:
    parsed = urlparse(url)
    return url if parsed.scheme and parsed.netloc else f"{base_url}/{url.lstrip('/')}"


def _response_request_url(response: Any, fallback: str) -> str:
    try:
        request = getattr(response, "request", None)
        request_url = getattr(request, "url", None)
        if request_url:
            return str(request_url)
    except (AttributeError, RuntimeError):
        pass
    return fallback


def _response_final_url(response: Any, fallback: str) -> str:
    """Read a response URL without assuming a fake response has a request."""

    try:
        value = getattr(response, "url", None)
        if value:
            return str(value)
    except (AttributeError, RuntimeError, ValueError):
        pass
    return fallback


def _response_bytes(response: Any) -> int:
    try:
        return len(bytes(getattr(response, "content", b"") or b""))
    except (TypeError, ValueError, RuntimeError):
        return 0


def _build_search_query(query: str | None, pushed_days: int | None) -> str:
    if not query:
        return ""
    normalized = query.strip()
    if pushed_days and not re.search(r"\bpushed:", normalized, flags=re.IGNORECASE):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=pushed_days)).date().isoformat()
        normalized = f"{normalized} pushed:>{cutoff}"
    return normalized


def _owner_repo(url: str) -> tuple[str | None, str | None]:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 4 and parts[0] == "repos":
        return parts[1], parts[2]
    return None, None


def _find_github_url(*values: Any) -> str | None:
    for value in values:
        if not value:
            continue
        match = re.search(
            r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
            str(value),
            flags=re.IGNORECASE,
        )
        if match:
            owner, repo = match.group(1), match.group(2).removesuffix(".git")
            return f"https://github.com/{owner}/{repo}".rstrip(".,)")
    return None


def _github_owner_repo_from_html_url(url: str) -> tuple[str | None, str | None]:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() in {"orgs", "users"}:
        return None, None
    return parts[0], parts[1].removesuffix(".git")


def _merge_github_metrics(metrics: dict[str, Any], payload: dict[str, Any]) -> None:
    aliases = {
        "stars": "stargazers_count",
        "forks": "forks_count",
        "pushed_at": "pushed_at",
        "language": "language",
        "archived": "archived",
        "fork": "fork",
        "license": "license",
    }
    for target, key in aliases.items():
        value = payload.get(key)
        if target == "license" and isinstance(value, dict):
            value = value.get("spdx_id") or value.get("name")
        if value is not None:
            metrics[target] = value
    metrics["github_metadata_fetched"] = True


def _copy_metric(metrics: dict[str, Any], payload: dict[str, Any], target: str, aliases: Iterable[str]) -> None:
    if target in metrics:
        return
    for alias in aliases:
        if payload.get(alias) is not None:
            value = payload[alias]
            # Feed extensions commonly expose counts as strings.  Keep the
            # canonical engagement fields numeric so policy comparisons and
            # ordering remain deterministic.
            if target in {"votes", "comments", "views", "likes", "reposts", "replies", "engagement"}:
                parsed = _number(value)
                metrics[target] = parsed if parsed is not None else value
            else:
                metrics[target] = value
            return


def _extract_feed_engagement(metrics: dict[str, Any], item: FetchItem) -> None:
    """Recover engagement fields exposed by feed extensions or rendered text."""

    payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    aliases = {
        "votes": ("votes", "votecount", "vote_count", "upvotes", "points", "ph_votes", "ph_vote_count"),
        "comments": ("comments", "commentcount", "comment_count", "comments_count", "ph_comments", "ph_comments_count", "ph_comment_count"),
    }
    flattened = {str(key).casefold().replace("-", "_"): value for key, value in payload.items()}
    for target, names in aliases.items():
        if target in metrics and metrics[target] is not None:
            continue
        for name in names:
            value = flattened.get(name)
            if value is not None:
                metrics[target] = _number(value)
                break
    text = " ".join(str(value) for value in (item.title, item.summary, item.content) if value)
    patterns = {
        "votes": r"(?:([0-9][0-9,]*)\s*(?:upvotes?|votes?)|(?:upvotes?|votes?)\s*[:：]?\s*([0-9][0-9,]*))",
        "comments": r"(?:([0-9][0-9,]*)\s*comments?|comments?\s*[:：]?\s*([0-9][0-9,]*))",
    }
    for target, pattern in patterns.items():
        if target in metrics:
            continue
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            raw = next((group for group in match.groups() if group), None)
            if raw is not None:
                metrics[target] = _number(raw)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = date_parser.parse(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> int | float | None:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().casefold().replace(",", "")
            multiplier = 1.0
            if text.endswith("k"):
                multiplier, text = 1_000.0, text[:-1]
            elif text.endswith("m"):
                multiplier, text = 1_000_000.0, text[:-1]
            number = float(text) * multiplier
            return int(number) if number.is_integer() else number
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _http_error_code(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in {401, 403}:
        return "auth_or_access_denied"
    if status in {404, 410, 422}:
        return "permanent_http_error"
    if status in {408, 425, 500, 502, 503, 504}:
        return "transient_http_error"
    return "http_error"


def _retryable(status: int, attempt: int, retries: int) -> bool:
    if attempt >= retries:
        return False
    if status in {401, 403, 404, 410, 422}:
        return False
    if status in {429, 503}:
        return attempt < 1
    return status in {408, 425, 500, 502, 504}


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return min(max(float(raw), 0.0), 60.0) if raw is not None else None
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return min(max((parsed - datetime.now(timezone.utc)).total_seconds(), 0.0), 60.0)
        except (TypeError, ValueError, OverflowError):
            return None
