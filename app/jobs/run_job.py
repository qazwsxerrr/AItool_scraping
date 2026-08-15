"""Fixed-order fetch -> AI review -> export orchestration.

    The normal ``run-once`` entry point is the complete AI-only orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from tempfile import TemporaryDirectory
from pathlib import Path

from app.config.limits import (
    DEFAULT_AI_REVIEW_LIMIT,
    DEFAULT_DAILY_REPORT_LIMIT,
    DEFAULT_FETCH_LIMIT_PER_SOURCE,
)
from app.config.settings import Settings
from app.jobs.ai_review_job import AIReviewResult, run_ai_review_from_settings
from app.jobs.editorial_rank_job import EditorialRankResult, run_editorial_rank_from_settings
from app.jobs.event_cluster_job import EventClusterResult, run_event_cluster_from_settings
from app.jobs.export_job import IntelExportResult, run_intel_export_from_settings
from app.jobs.fetch_job import IntelFetchResult, run_intel_fetch_from_settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelCounts, IntelRepository


@dataclass(frozen=True)
class IntelRunResult:
    run_id: int | None
    fetch: IntelFetchResult
    ai_review: AIReviewResult
    export: IntelExportResult
    status: str
    error: str | None = None
    event_cluster: EventClusterResult | None = None
    editorial_rank: EditorialRankResult | None = None

def run_intel_once_from_settings(
    *,
    settings: Settings,
    source: str | None = None,
    content_class: str | None = None,
    limit: int = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    ai_limit: int = DEFAULT_AI_REVIEW_LIMIT,
    force: bool = False,
    dry_run: bool = False,
    output_dir: str = "output/intel",
    profile_path: str | Path | None = None,
    snapshot_key: str = "latest",
    ai_client: object | None = None,
) -> IntelRunResult:
    """Run the fixed-order pipeline with independent fetch and review limits.

    ``ai_client`` is primarily a deterministic test/integration seam; when it
    is omitted the settings boundary constructs ``IntelTriageClient``.
    ``limit`` controls the per-source fetch request, while ``ai_limit`` controls
    the existing-item review, event aggregation, and editorial ranking pool.
    """
    if dry_run:
        # Use one ephemeral SQLite file for the three stages.  The fetch stage
        # may populate it so the following stages can observe the same batch, while
        # the caller's configured database and output directory remain untouched.
        with TemporaryDirectory(prefix="intel-dry-run-") as temp_dir:
            ephemeral = replace(settings, database_url=f"sqlite:///{Path(temp_dir) / 'intel.db'}")
            fetch = run_intel_fetch_from_settings(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit_per_source=limit,
                force=force,
                dry_run=False,
            )
            ai_review = run_ai_review_from_settings(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit=ai_limit,
                force=force,
                dry_run=True,
                ai_client=ai_client,
            )
            event_cluster = run_event_cluster_from_settings(
                settings=ephemeral,
                limit=ai_limit,
                force=force,
                snapshot_key=snapshot_key,
            )
            editorial_rank = run_editorial_rank_from_settings(
                settings=ephemeral,
                profile_path=profile_path,
                limit=ai_limit,
                force=force,
                snapshot_key=snapshot_key,
            )
            export = run_intel_export_from_settings(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit=DEFAULT_DAILY_REPORT_LIMIT,
                output_dir=output_dir,
                dry_run=True,
                snapshot_key=snapshot_key,
            )
        fetch = replace(fetch, dry_run=True)
        return IntelRunResult(None, fetch, ai_review, export, "dry_run", None, event_cluster, editorial_rank)

    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run = IntelRepository(session).start_run(
            filters={"source": source, "content_class": content_class, "stage": "run-once"}
        )
        session.commit()
        run_id = run.id

    try:
        fetch = run_intel_fetch_from_settings(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit_per_source=limit,
            force=force,
            run_id=run_id,
        )
        ai_review = run_ai_review_from_settings(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit=ai_limit,
            force=force,
            run_id=run_id,
            ai_client=ai_client,
        )
        event_cluster = run_event_cluster_from_settings(
            settings=settings,
            limit=ai_limit,
            force=force,
            snapshot_key=snapshot_key,
            run_id=run_id,
        )
        editorial_rank = run_editorial_rank_from_settings(
            settings=settings,
            profile_path=profile_path,
            limit=ai_limit,
            force=force,
            snapshot_key=snapshot_key,
            run_id=run_id,
        )
        export = run_intel_export_from_settings(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit=DEFAULT_DAILY_REPORT_LIMIT,
            output_dir=output_dir,
            snapshot_key=snapshot_key,
        )
        # ``failed`` counts each failed item once; ``ai_failed`` is the
        # narrower audit counter for model failures and is already included.
        status = "completed_with_errors" if (fetch.total_failed or ai_review.failed) else "completed"
        with session_factory() as session:
            IntelRepository(session).finish_run(
                run_id,
                status=status,
                counts=IntelCounts(
                    fetched=fetch.total_fetched,
                    inserted=fetch.total_inserted,
                    skipped=fetch.total_skipped,
                    selected=ai_review.selected,
                    analyzed=ai_review.analyzed,
                    failed=fetch.total_failed + ai_review.failed,
                ),
            )
            session.commit()
        return IntelRunResult(run_id, fetch, ai_review, export, status, None, event_cluster, editorial_rank)
    except Exception as exc:
        with session_factory() as session:
            IntelRepository(session).finish_run(run_id, status="failed", error=str(exc))
            session.commit()
        empty_fetch = IntelFetchResult(run_id=run_id)
        empty_ai_review = AIReviewResult()
        empty_export = IntelExportResult(0, 0, f"{output_dir}/intel_items.jsonl", f"{output_dir}/intel_digest.md", f"{output_dir}/intel_pending.jsonl")
        return IntelRunResult(run_id, empty_fetch, empty_ai_review, empty_export, "failed", str(exc), None, None)
