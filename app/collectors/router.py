"""Strict transport router for the collector boundary."""

from __future__ import annotations

from app.domain.models import FetchBatch, SourceSpec

from .base import Collector
from .feed import FeedCollector, ProductHuntCollector, RSSHubCollector
from .github import GitHubCollector, GitHubTrendingCollector


class CollectorRouter:
    """Resolve a source using only its canonical transport/options.

    The router deliberately has no default branch.  An invalid or incomplete
    source must be surfaced to the caller instead of silently becoming RSS.
    """

    def __init__(
        self,
        *,
        feed: Collector,
        rsshub: Collector | None = None,
        github: Collector,
        github_trending: Collector,
        producthunt: Collector,
    ) -> None:
        self.feed = feed
        # RSSHub documents are parsed exactly like native feeds.  Keep the
        # optional name for callers migrating from the old constructor while
        # honoring a separately configured local RSSHub client when provided.
        self.rsshub = rsshub if rsshub is not None else feed
        self.github = github
        self.github_trending = github_trending
        self.producthunt = producthunt

    def collector_for(self, source: SourceSpec) -> Collector:
        if source.transport == "feed":
            feed = source.feed
            if feed is None:
                raise ValueError(f"feed source {source.id} requires feed options")
            if feed.adapter == "generic":
                return self.feed
            if feed.adapter == "producthunt":
                return self.producthunt
            raise ValueError(f"unsupported feed adapter for {source.id}: {feed.adapter}")
        if source.transport == "rsshub":
            # Do not duplicate RSSHub parsing/transport behavior.
            return self.rsshub
        if source.transport == "github":
            options = source.github
            if options is None:
                raise ValueError(f"GitHub source {source.id} requires github options")
            if options.mode == "trending":
                return self.github_trending
            if options.mode in {"search", "releases"}:
                return self.github
            raise ValueError(f"unsupported GitHub mode for {source.id}: {options.mode}")
        raise ValueError(f"unsupported source transport for {source.id}: {source.transport}")

    def collect(self, source: SourceSpec, limit: int) -> FetchBatch:
        return self.collector_for(source).collect(source, limit)


__all__ = ["CollectorRouter"]
