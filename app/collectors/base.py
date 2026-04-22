from __future__ import annotations

from typing import Protocol

from app.config.source_registry import SourceConfig
from app.parsers.feed_parser import ParsedFeedItem


class FeedCollector(Protocol):
    def collect(self, source: SourceConfig, limit: int | None = None) -> list[ParsedFeedItem]:
        """Collect and parse items for one source."""
