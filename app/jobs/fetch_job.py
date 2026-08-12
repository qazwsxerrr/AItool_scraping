"""Fetch stage for the simplified intelligence pipeline."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.feed import FeedCollector, ProductHuntCollector, RSSHubCollector
from app.collectors.github import GitHubCollector, GitHubTrendingCollector
from app.collectors.router import CollectorRouter
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, load_source_registry
from app.domain.models import FetchBatch, SourceSpec
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelCounts, IntelRepository
from app.storage.models import FetchAttempt, Source

LOGGER = logging.getLogger(__name__)


@dataclass
class IntelSourceStats:
    source_id: str
    content_class: str | None = None
    status: str = "skipped"
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    selected: int = 0
    http_status: int | None = None
    response_bytes: int = 0
    retry_count: int = 0
    transport: str | None = None
    error: str | None = None
    attempt_id: int | None = None
    duration_seconds: float | None = None


@dataclass
class IntelFetchResult:
    stats: dict[str, IntelSourceStats] = field(default_factory=dict)
    skipped_sources: list[str] = field(default_factory=list)
    not_due_sources: list[str] = field(default_factory=list)
    run_id: int | None = None
    dry_run: bool = False

    @property
    def total_fetched(self) -> int:
        return sum(v.fetched for v in self.stats.values())

    @property
    def total_inserted(self) -> int:
        return sum(v.inserted for v in self.stats.values())

    @property
    def total_skipped(self) -> int:
        return sum(v.skipped for v in self.stats.values())

    @property
    def total_failed(self) -> int:
        return sum(v.failed for v in self.stats.values())


def run_intel_fetch_job(
    *,
    session_factory: sessionmaker[Session],
    sources: Iterable[SourceSpec],
    router: CollectorRouter,
    limit_per_source: int | None = None,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    run_id: int | None = None,
) -> IntelFetchResult:
    """Fetch due sources with one shared client and source-level isolation."""

    specs = list(sources)
    selected = [
        spec
        for spec in specs
        if (source_filter is None or spec.id == source_filter)
        and (content_class is None or spec.content_class == content_class)
    ]
    result = IntelFetchResult(run_id=run_id, dry_run=dry_run)
    own_run = run_id is None and not dry_run

    if own_run:
        with session_factory() as session:
            run = IntelRepository(session).start_run(
                filters={"source": source_filter, "content_class": content_class, "stage": "fetch"}
            )
            session.commit()
            result.run_id = run.id

    source_state: dict[str, tuple[Source, FetchAttempt | None]] = {}
    if not dry_run:
        with session_factory() as session:
            repo = IntelRepository(session)
            for spec in selected:
                policy = spec
                source_row = repo.upsert_source(spec, policy=policy)
                latest = session.scalars(
                    select(FetchAttempt)
                    .where(FetchAttempt.source_id == spec.id)
                    .order_by(FetchAttempt.started_at.desc(), FetchAttempt.id.desc())
                    .limit(1)
                ).first()
                source_state[spec.id] = (source_row, latest)
            session.commit()

    for spec in selected:
        stats = IntelSourceStats(source_id=spec.id, content_class=spec.content_class)
        result.stats[spec.id] = stats
        if not dry_run:
            source_row, latest = source_state[spec.id]
            if not force and not _is_due(source_row, latest):
                result.not_due_sources.append(spec.id)
                stats.status = "skipped"
                with session_factory() as session:
                    repo = IntelRepository(session)
                    attempt = repo.create_attempt(
                        source_id=spec.id,
                        request_url=_safe_url(spec.url),
                        run_id=result.run_id,
                        manual_override=False,
                    )
                    repo.finish_attempt(
                        attempt.id,
                        status="skipped",
                        items_fetched=0,
                        error="fetch_interval_not_elapsed",
                    )
                    stats.attempt_id = attempt.id
                    session.commit()
                continue

        started = time.monotonic()
        attempt_id: int | None = None
        try:
            if not dry_run:
                with session_factory() as session:
                    attempt = IntelRepository(session).create_attempt(
                        source_id=spec.id,
                        request_url=_safe_url(spec.url),
                        run_id=result.run_id,
                        manual_override=force,
                    )
                    session.commit()
                    attempt_id = attempt.id
                    stats.attempt_id = attempt_id

            batch = router.collect(spec, limit_per_source or spec.default_limit)
            _apply_batch_stats(stats, batch)
            if batch.status == "failed":
                stats.failed = 1
                stats.status = "failed"
                stats.error = batch.error_message
                if not dry_run:
                    _finish_attempt(session_factory, attempt_id, batch=batch, status="failed", error=batch.error_message)
                continue
            stats.status = batch.status
            stats.fetched = len(batch.items)
            if not dry_run:
                with session_factory() as session:
                    repo = IntelRepository(session)
                    for raw_item in batch.items:
                        item = raw_item.model_copy(update={"content_class": spec.content_class})
                        try:
                            with session.begin_nested():
                                insert = repo.insert_item(item)
                            if insert.inserted:
                                stats.inserted += 1
                            else:
                                stats.skipped += 1
                        except Exception as exc:
                            stats.failed += 1
                            LOGGER.warning("intel item insert failed source=%s: %s", spec.id, exc)
                    source = session.get(Source, spec.id)
                    if source is not None:
                        # A 304 is still a successful request and must advance
                        # the cooldown, otherwise it would be retried every run.
                        source.last_fetched_at = datetime.now(timezone.utc)
                    repo.finish_attempt(
                        attempt_id or 0,
                        status=batch.status,
                        metadata=batch,
                        items_fetched=stats.fetched,
                        items_inserted=stats.inserted,
                        items_skipped=stats.skipped,
                    )
                    session.commit()
            else:
                stats.inserted = stats.fetched
        except Exception as exc:
            stats.failed = max(1, stats.failed)
            stats.status = "failed"
            stats.error = str(exc)[:4000]
            LOGGER.exception("intel source fetch failed: %s", spec.id)
            if not dry_run:
                _finish_attempt(session_factory, attempt_id, batch=None, status="failed", error=exc)
        finally:
            stats.duration_seconds = time.monotonic() - started

    if not dry_run and result.run_id is not None and own_run:
        with session_factory() as session:
            counts = IntelCounts(
                fetched=result.total_fetched,
                inserted=result.total_inserted,
                skipped=result.total_skipped,
                failed=result.total_failed,
            )
            run_status = "completed_with_errors" if result.total_failed else "completed"
            IntelRepository(session).finish_run(result.run_id, status=run_status, counts=counts)
            session.commit()
    return result


def run_intel_fetch_from_settings(
    *,
    settings: Settings,
    registry_path=DEFAULT_REGISTRY_PATH,
    limit_per_source: int | None = None,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    run_id: int | None = None,
) -> IntelFetchResult:
    registry = load_source_registry(registry_path, env={"RSSHUB_BASE_URL": settings.rsshub_base_url or ""})
    # A fetch dry-run never needs the target database; use an in-memory schema
    # so even creating a previously absent SQLite file is avoided.
    database_url = "sqlite:///:memory:" if dry_run else settings.database_url
    engine = create_engine_from_url(database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    external_client = _build_http_client(settings, trust_env=True)
    # The active RSSHub deployment is a local Node process. Its outbound X
    # proxy is configured by scripts/start_rsshub.sh; Python calls the
    # configured RSSHUB_BASE_URL directly instead of inheriting HTTP_PROXY.
    rsshub_client = _build_http_client(settings, trust_env=False)
    try:
        feed = FeedCollector(external_client, retries=settings.request_retries, user_agent=settings.user_agent)
        rsshub = RSSHubCollector(rsshub_client, retries=settings.request_retries, user_agent=settings.user_agent)
        github = GitHubCollector(
            external_client,
            base_url=settings.github_api_base_url,
            token=settings.github_api_token,
            api_version=settings.github_api_version,
            user_agent=settings.user_agent,
            retries=settings.request_retries,
            timeout_seconds=settings.github_timeout_seconds,
        )
        github_trending = GitHubTrendingCollector(
            external_client,
            retries=settings.request_retries,
            user_agent=settings.user_agent,
        )
        producthunt = ProductHuntCollector(
            external_client,
            retries=settings.request_retries,
            user_agent=settings.user_agent,
            github_lookup=github.lookup_repository,
        )
        router = CollectorRouter(
            feed=feed,
            rsshub=rsshub,
            github=github,
            github_trending=github_trending,
            producthunt=producthunt,
        )
        result = run_intel_fetch_job(
            session_factory=session_factory,
            sources=registry.sources,
            router=router,
            limit_per_source=limit_per_source,
            source_filter=source_filter,
            content_class=content_class,
            force=force,
            dry_run=dry_run,
            run_id=run_id,
        )
    finally:
        external_client.close()
        rsshub_client.close()
    result.skipped_sources.extend(f"{item.source_id}: {item.reason}" for item in registry.skipped)
    return result


def _build_http_client(settings: Settings, *, trust_env: bool) -> httpx.Client:
    """Build one of the two fixed fetch profiles.

    ``trust_env=True`` is used for external native feeds such as Reddit and
    LINUX DO, so they use the configured 2080 proxy.  ``trust_env=False`` is
    used for the local RSSHub endpoint, whose Node process owns X's outbound
    proxy configuration.
    """

    return httpx.Client(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        http2=True,
        trust_env=trust_env,
        headers={"User-Agent": settings.user_agent},
    )


def _apply_batch_stats(stats: IntelSourceStats, batch: FetchBatch) -> None:
    stats.http_status = batch.http_status
    stats.response_bytes = batch.response_bytes
    stats.retry_count = batch.retry_count
    stats.transport = batch.transport


def _finish_attempt(
    session_factory: sessionmaker[Session],
    attempt_id: int | None,
    *,
    batch: FetchBatch | None,
    status: str,
    error: Exception | str | None = None,
) -> None:
    if attempt_id is None:
        return
    with session_factory() as session:
        repo = IntelRepository(session)
        attempt = session.get(FetchAttempt, attempt_id)
        repo.finish_attempt(
            attempt_id,
            status=status,
            metadata=batch,
            items_fetched=len(batch.items) if batch else 0,
            error=error,
        )
        # Failed requests also enter the source cooldown.  Keep skipped
        # cooldown checks from erasing that failure timestamp on the next run.
        if attempt is not None and status != "skipped":
            source = session.get(Source, attempt.source_id)
            if source is not None:
                source.last_fetched_at = datetime.now(timezone.utc)
        session.commit()


def _is_due(source: Source, latest: FetchAttempt | None) -> bool:
    last = _as_utc(source.last_fetched_at)
    if latest is not None and latest.status == "failed":
        failure = _as_utc(latest.finished_at or latest.started_at)
        if failure and (last is None or failure > last):
            last = failure
    if last is None:
        return True
    return datetime.now(timezone.utc) >= last + timedelta(seconds=max(source.fetch_interval, 1))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url[:2000]
    # Keep query parameters because RSSHub/Reddit search URLs and GitHub
    # request URLs are part of the replayable fetch metadata. Fragments are
    # client-only and are intentionally removed; obvious credential values are
    # redacted before persistence.
    query = parsed.query
    if query:
        import re
        from urllib.parse import parse_qsl, urlencode

        pairs = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            if re.search(r"(?:token|secret|password|api[_-]?key|cookie|auth)", key, re.IGNORECASE):
                value = "[REDACTED]"
            pairs.append((key, value))
        query = urlencode(pairs, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))[:2000]
