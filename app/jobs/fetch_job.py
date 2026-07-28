from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import FeedCollector
from app.collectors.github_collector import GitHubAPICollector
from app.collectors.rss_collector import HTTPFeedCollector
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, SourceConfig, load_source_registry
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import RawItemRepository, SourceRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class SourceFetchStats:
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None


@dataclass
class FetchJobResult:
    stats: dict[str, SourceFetchStats] = field(default_factory=dict)
    skipped_sources: list[str] = field(default_factory=list)

    @property
    def total_fetched(self) -> int:
        return sum(item.fetched for item in self.stats.values())

    @property
    def total_inserted(self) -> int:
        return sum(item.inserted for item in self.stats.values())

    @property
    def total_skipped(self) -> int:
        return sum(item.skipped for item in self.stats.values())

    @property
    def total_failed(self) -> int:
        return sum(item.failed for item in self.stats.values())


class SourceCollectorRouter:
    """Route source configs to the collector that understands their source type."""

    def __init__(self, *, feed_collector: FeedCollector, github_collector: FeedCollector) -> None:
        self.feed_collector = feed_collector
        self.github_collector = github_collector

    def collect(self, source: SourceConfig, limit: int | None = None):
        if source.type in {"rss", "atom", "rsshub"}:
            return self.feed_collector.collect(source, limit=limit)
        if source.type == "github_api":
            return self.github_collector.collect(source, limit=limit)
        raise ValueError(f"unsupported source type: {source.type}")


def run_fetch_job(
    *,
    session_factory: sessionmaker[Session],
    sources: Iterable[SourceConfig],
    collector: FeedCollector | None = None,
    limit_per_source: int | None = None,
    source_filter: str | None = None,
    source_group_filter: str | None = None,
) -> FetchJobResult:
    """Fetch enabled sources and persist new raw_items; source failures are isolated."""
    selected_sources = [
        source
        for source in sources
        if source_filter in {None, source.id}
        and source_group_filter in {None, source.source_group}
    ]
    result = FetchJobResult()
    feed_collector = collector or HTTPFeedCollector()

    with session_factory() as session:
        source_repo = SourceRepository(session)
        for source in selected_sources:
            source_repo.upsert_source(source)
        session.commit()

    for source in selected_sources:
        stats = SourceFetchStats()
        result.stats[source.id] = stats
        try:
            effective_limit = limit_per_source if limit_per_source is not None else source.default_limit
            items = feed_collector.collect(source, limit=effective_limit)
            stats.fetched = len(items)
            with session_factory() as session:
                raw_repo = RawItemRepository(session)
                source_repo = SourceRepository(session)
                for item in items:
                    insert_result = raw_repo.insert_if_new(item)
                    if insert_result.inserted:
                        stats.inserted += 1
                    else:
                        stats.skipped += 1
                source_repo.mark_fetched(source.id)
                session.commit()
            LOGGER.info(
                "Fetched source %s: fetched=%s inserted=%s skipped=%s",
                source.id,
                stats.fetched,
                stats.inserted,
                stats.skipped,
            )
        except Exception as exc:  # source-level isolation is intentional here
            stats.failed = 1
            stats.error = str(exc)
            LOGGER.exception("Source fetch failed: %s", source.id)
    return result


def run_fetch_from_registry(
    *,
    settings: Settings,
    registry_path=DEFAULT_REGISTRY_PATH,
    limit_per_source: int | None = None,
    source_filter: str | None = None,
    source_group_filter: str | None = None,
) -> FetchJobResult:
    registry = load_source_registry(registry_path, env={"RSSHUB_BASE_URL": settings.rsshub_base_url or ""})
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    feed_collector = HTTPFeedCollector(
        timeout_seconds=settings.request_timeout_seconds,
        retries=settings.request_retries,
        user_agent=settings.user_agent,
    )
    github_collector = GitHubAPICollector(
        base_url=settings.github_api_base_url,
        token=settings.github_api_token,
        api_version=settings.github_api_version,
        timeout_seconds=settings.github_timeout_seconds,
        user_agent=settings.user_agent,
    )
    collector = SourceCollectorRouter(feed_collector=feed_collector, github_collector=github_collector)
    result = run_fetch_job(
        session_factory=session_factory,
        sources=registry.sources,
        collector=collector,
        limit_per_source=limit_per_source,
        source_filter=source_filter,
        source_group_filter=source_group_filter,
    )
    result.skipped_sources.extend(f"{item.source_id}: {item.reason}" for item in registry.skipped)
    return result
