"""Compatibility exports for the pre-split collector module.

New code should import from ``feed``, ``github``, or ``router`` directly.
Keeping this small shim avoids breaking downstream callers while the
ingestion jobs migrate away from the former monolithic implementation.
"""

from .feed import FeedCollector, ProductHuntCollector, RSSCollector, RSSHubCollector
from .github import GitHubCollector, GitHubTrendingCollector
from .http import HTTPClient, RequestFailure as _RequestFailure
from .router import CollectorRouter

Collector = FeedCollector

__all__ = [
    "Collector",
    "CollectorRouter",
    "FeedCollector",
    "GitHubCollector",
    "GitHubTrendingCollector",
    "ProductHuntCollector",
    "RSSCollector",
    "RSSHubCollector",
    "HTTPClient",
]
