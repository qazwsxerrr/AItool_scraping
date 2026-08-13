"""Fixed-order fetch -> AI review -> export orchestration.

The explicit ``process`` command remains available as a legacy verification
path; the normal ``run-once`` entry point intentionally does not invoke it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Any

from app.config.settings import Settings
from app.jobs.ai_review_job import AIReviewResult, run_ai_review_from_settings
from app.jobs.export_job import IntelExportResult, run_intel_export_from_settings
from app.jobs.fetch_job import IntelFetchResult, run_intel_fetch_from_settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelCounts, IntelRepository


@dataclass(frozen=True)
class IntelRunResult:
    run_id: int | None
    fetch: IntelFetchResult
    process: AIReviewResult
    export: IntelExportResult
    status: str
    error: str | None = None

    @property
    def ai_review(self) -> AIReviewResult:
        return self.process


def run_intel_once_from_settings(
    *,
    settings: Settings,
    source: str | None = None,
    content_class: str | None = None,
    limit: int = 100,
    force: bool = False,
    dry_run: bool = False,
    output_dir: str = "output/intel",
) -> IntelRunResult:
    if dry_run:
        # Use one ephemeral SQLite file for the three stages.  The fetch stage
        # may populate it so process/export can observe the same batch, while
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
            process = run_ai_review_from_settings(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit=limit,
                force=force,
                dry_run=True,
            )
            export = run_intel_export_from_settings(
                settings=ephemeral,
                source_filter=source,
                content_class=content_class,
                limit=limit,
                output_dir=output_dir,
                dry_run=True,
            )
        fetch = replace(fetch, dry_run=True)
        return IntelRunResult(None, fetch, process, export, "dry_run")

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
        process = run_ai_review_from_settings(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit=limit,
            force=force,
            run_id=run_id,
        )
        export = run_intel_export_from_settings(
            settings=settings,
            source_filter=source,
            content_class=content_class,
            limit=limit,
            output_dir=output_dir,
        )
        # ``failed`` counts each failed item once; ``ai_failed`` is the
        # narrower audit counter for model failures and is already included.
        status = "completed_with_errors" if (fetch.total_failed or process.failed) else "completed"
        with session_factory() as session:
            IntelRepository(session).finish_run(
                run_id,
                status=status,
                counts=IntelCounts(
                    fetched=fetch.total_fetched,
                    inserted=fetch.total_inserted,
                    skipped=fetch.total_skipped,
                    selected=process.selected,
                    analyzed=process.analyzed,
                    verified=0,
                    failed=fetch.total_failed + process.failed,
                ),
            )
            session.commit()
        return IntelRunResult(run_id, fetch, process, export, status)
    except Exception as exc:
        with session_factory() as session:
            IntelRepository(session).finish_run(run_id, status="failed", error=str(exc))
            session.commit()
        empty_fetch = IntelFetchResult(run_id=run_id)
        empty_process = AIReviewResult()
        empty_export = IntelExportResult(0, 0, f"{output_dir}/intel_items.jsonl", f"{output_dir}/intel_digest.md", f"{output_dir}/intel_pending.jsonl")
        return IntelRunResult(run_id, empty_fetch, empty_process, empty_export, "failed", str(exc))
