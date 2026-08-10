"""Collector public API."""

from .feed import FeedCollector, ProductHuntCollector, RSSCollector, RSSHubCollector
from .github import GitHubCollector, GitHubTrendingCollector
from .router import CollectorRouter

__all__ = [
    "CollectorRouter",
    "FeedCollector",
    "GitHubCollector",
    "GitHubTrendingCollector",
    "ProductHuntCollector",
    "RSSCollector",
    "RSSHubCollector",
]
