"""Native RSS/Atom and RSSHub collectors.

RSSHub serves ordinary RSS/Atom documents, so both transports intentionally
share :class:`FeedCollector`.  Product Hunt is a small Atom adapter layered on
the same GET-and-parse path; it never calls a Product Hunt API.
"""

from __future__ import annotations

import json
import time
import re
from urllib.parse import urljoin
from typing import Any, Mapping

from app.config.settings import DEFAULT_USER_AGENT
from app.content_extraction import _block_text
from app.domain.models import FetchBatch, FetchItem, SourceSpec
from app.parsers.feed_parser import html_to_text, parse_feed
from bs4 import BeautifulSoup

from .base import Collector
from .common import (
    copy_metric,
    extract_feed_engagement,
    find_github_url,
    github_owner_repo_from_html_url,
    merge_github_metrics,
)
from .http import (
    HTTPClient,
    failed_batch,
    request_with_retry,
    response_final_url,
    response_request_url,
)


class FeedCollector(Collector):
    """Collect one RSS or Atom document using the caller-provided client."""

    def __init__(
        self,
        client: HTTPClient,
        *,
        retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
        max_response_bytes: int = 2 * 1024 * 1024,
        timeout_seconds: float | None = None,
        sleeper=time.sleep,
    ) -> None:
        self.client = client
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.max_response_bytes = max_response_bytes
        self.timeout_seconds = timeout_seconds
        self.sleeper = sleeper

    def collect(
        self,
        source: SourceSpec,
        limit: int,
        request_headers: Mapping[str, str] | None = None,
    ) -> FetchBatch:
        if not source.url:
            return failed_batch(source, "missing_url", "source has no URL")
        if source.feed and source.feed.adapter == "anthropic_research":
            return self._collect_anthropic_research(source, limit, request_headers)
        # Keep the registry URL unchanged. Reddit's standard ``.rss`` route
        # already returns Atom XML; query-string workarounds such as
        # ``raw_json=1`` can trigger a different 403/429 edge path.
        request_url = source.url
        response, retry_count, error = request_with_retry(
            self.client,
            request_url,
            retries=self.retries,
            user_agent=self.user_agent,
            max_response_bytes=self.max_response_bytes,
            extra_headers=dict(request_headers or {}),
            timeout_seconds=self.timeout_seconds,
            sleeper=self.sleeper,
        )
        if error is not None:
            return failed_batch(
                source,
                error[0],
                error[1],
                http_status=error[2],
                response_bytes=getattr(error, "response_bytes", 0),
                retry_count=retry_count,
                request_url=request_url,
            )
        assert response is not None
        final_url = response_final_url(response, request_url)
        request_url = response_request_url(response, request_url)
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
                etag=_response_header(response, "etag"),
                last_modified=_response_header(response, "last-modified"),
                not_modified=True,
            )
        body = bytes(getattr(response, "content", b""))
        try:
            feed_format = source.feed.format if source.feed is not None else None
            parsed = parse_feed(body, source_id=source.id, feed_format=feed_format)
        except Exception as exc:
            return failed_batch(
                source,
                "parse_error",
                str(exc),
                http_status=status_code,
                retry_count=retry_count,
                request_url=request_url,
                final_url=final_url,
                response_bytes=len(body),
            )
        items = [_feed_item_to_domain(item, source) for item in parsed[: max(0, int(limit))]]
        status = "success"
        error_code = None
        error_message = None
        if not items and _is_rsshub_x_source(source):
            status = "degraded"
            error_code = "empty_feed"
            error_message = "RSSHub X returned a valid feed with no entries"
        return FetchBatch(
            source=source,
            items=items,
            status=status,
            http_status=status_code,
            request_url=request_url,
            final_url=final_url,
            response_bytes=len(body),
            retry_count=retry_count,
            transport="httpx",
            etag=_response_header(response, "etag"),
            last_modified=_response_header(response, "last-modified"),
            error_code=error_code,
            error_message=error_message,
        )

    def _collect_anthropic_research(self, source, limit, request_headers):
        response, retry_count, error = request_with_retry(
            self.client, source.url, retries=self.retries, user_agent=self.user_agent,
            extra_headers={"Accept": "text/html,application/xhtml+xml", **dict(request_headers or {})},
            timeout_seconds=self.timeout_seconds, sleeper=self.sleeper,
        )
        if error is not None or response is None:
            return failed_batch(source, error[0], error[1], http_status=error[2], retry_count=retry_count)
        listing = BeautifulSoup(bytes(response.content), "html.parser")
        items = []
        for anchor in listing.select('a[href*="/research/"]')[:limit]:
            url = urljoin(source.url, anchor.get("href", ""))
            article_response, _, article_error = request_with_retry(
                self.client, url, retries=self.retries, user_agent=self.user_agent,
                extra_headers={"Accept": "text/html,application/xhtml+xml"},
                timeout_seconds=self.timeout_seconds, sleeper=self.sleeper,
            )
            if article_error or article_response is None:
                continue
            page = BeautifulSoup(bytes(article_response.content), "html.parser")
            content = _block_text(page.select_one("main") or page.select_one("article"))
            if content:
                items.append(FetchItem(
                    source_id=source.id,
                    external_id=url,
                    title=re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip(),
                    url=url,
                    canonical_url=url,
                    summary=content,
                    content=content,
                    content_depth="full",
                    raw_payload={"listing_url": source.url},
                    kind="feed",
                ))
        return FetchBatch(source=source, items=items, status="success", retry_count=retry_count, transport="httpx")


class ProductHuntCollector(FeedCollector):
    """Map Product Hunt's public Atom feed without API or GraphQL fallback."""

    def __init__(self, *args: Any, github_lookup=None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.github_lookup = github_lookup

    def collect(
        self,
        source: SourceSpec,
        limit: int,
        request_headers: Mapping[str, str] | None = None,
    ) -> FetchBatch:
        feed = source.feed
        if source.transport != "feed" or feed is None or feed.format != "atom":
            return failed_batch(
                source,
                "invalid_source",
                "Product Hunt collector requires transport=feed and feed.format=atom",
            )
        batch = super().collect(source, limit, request_headers=request_headers)
        for item in batch.items:
            payload = dict(item.raw_payload)
            metrics = dict(item.metrics)
            copy_metric(
                metrics,
                payload,
                "votes",
                ("votes", "voteCount", "vote_count", "points", "upvotes", "ph_vote_count", "ph_votes"),
            )
            copy_metric(
                metrics,
                payload,
                "comments",
                ("comments", "commentCount", "comment_count", "comments_count", "ph_comments_count", "ph_comment_count"),
            )
            extract_feed_engagement(
                metrics,
                title=item.title,
                summary=item.summary,
                content=item.content,
                payload=payload,
            )
            if "votes" not in metrics and "comments" not in metrics:
                metrics["producthunt_metrics_status"] = "unavailable_in_feed"
            else:
                metrics["producthunt_metrics_status"] = "available"
            github_url = find_github_url(
                item.url,
                item.content,
                item.summary,
                payload.get("website"),
                json.dumps(payload.get("productLinks") or payload.get("product_links") or [], default=str),
            )
            if github_url:
                metrics.setdefault("github_url", github_url)
                owner, repo = github_owner_repo_from_html_url(github_url)
                if owner and repo:
                    metrics.setdefault("github_owner", owner)
                    metrics.setdefault("github_repo", repo)
                    metrics.setdefault("canonical_project_key", f"{owner}/{repo}")
                    item.external_id = f"github_repo:{owner.casefold()}/{repo.casefold()}"
                    if self.github_lookup is not None:
                        try:
                            github_payload = self.github_lookup(owner, repo)
                            if isinstance(github_payload, dict):
                                merge_github_metrics(metrics, github_payload)
                                item.external_id = f"github_repo:{owner.casefold()}/{repo.casefold()}"
                        except Exception as exc:
                            metrics.setdefault("github_enrichment_error", type(exc).__name__)
            item.metrics = metrics
            item.raw_payload = payload
        return batch


def _feed_item_to_domain(item: Any, source: SourceSpec) -> FetchItem:
    payload = dict(getattr(item, "raw_payload", {}) or {})
    summary = getattr(item, "raw_summary", None)
    content = getattr(item, "raw_content", None)
    content_depth = getattr(item, "content_depth", None)
    if content:
        content = html_to_text(content)
        content_depth = "full" if content else ("summary" if summary else "missing")
    if source.id == "google_blog_ai":
        content = None
        content_depth = "summary" if summary else "missing"
    if source.id == "openai_news":
        content = None
        content_depth = "summary" if summary else "missing"
    if source.transport == "rsshub" or source.id == "linux_do_hot":
        content = html_to_text(summary)
        content_depth = "full" if content else "missing"
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
    return FetchItem(
        source_id=source.id,
        external_id=getattr(item, "external_id", None),
        title=getattr(item, "title", "(untitled)"),
        url=getattr(item, "link", None),
        author=getattr(item, "author", None),
        published_at=getattr(item, "published_at", None),
        summary=summary,
        content=content,
        content_depth=content_depth,
        metrics=metrics,
        raw_payload=payload,
        kind="feed",
    )


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    try:
        value = headers.get(name) or headers.get(name.title())
    except (AttributeError, TypeError):
        return None
    text = str(value).strip() if value is not None else ""
    return text or None


def _is_rsshub_x_source(source: SourceSpec) -> bool:
    return source.transport == "rsshub" and str(source.source_group or "").casefold() in {
        "x_official",
        "x_social",
        "x_search",
    }


__all__ = ["FeedCollector", "ProductHuntCollector"]
