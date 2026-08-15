"""Compatibility facade for the historical ``run-once`` command.

The durable control-plane implementation lives in :mod:`pipeline_orchestrator`.
This module keeps the old import and monkeypatch seams used by integrations and
tests, then delegates the actual run to that orchestrator.
"""

from __future__ import annotations

from pathlib import Path

from app.config.limits import (
    DEFAULT_FETCH_LIMIT_PER_SOURCE,
)
from app.config.settings import Settings
from app.jobs.ai_review_job import AIReviewResult, run_ai_review_from_settings
from app.jobs.editorial_rank_job import EditorialRankResult, run_editorial_rank_from_settings
from app.jobs.event_cluster_job import EventClusterResult, run_event_cluster_from_settings
from app.jobs.export_job import IntelExportResult, run_intel_export_from_settings
from app.jobs.fetch_job import IntelFetchResult, run_intel_fetch_from_settings
from app.jobs.pipeline_orchestrator import PipelineRunResult, run_pipeline_once_from_settings


IntelRunResult = PipelineRunResult

def run_intel_once_from_settings(
    *,
    settings: Settings,
    source: str | None = None,
    content_class: str | None = None,
    limit: int = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    ai_limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    output_dir: str = "output/intel",
    profile_path: str | Path | None = None,
    snapshot_key: str | None = None,
    ai_client: object | None = None,
) -> IntelRunResult:
    """Run the complete convenience pipeline through the durable orchestrator.

    The explicit runner arguments preserve the historical test/integration
    injection seam: monkeypatching a runner on ``app.jobs.run_job`` still
    affects this facade, while all run creation and final accounting are now
    centralized in ``pipeline_orchestrator``.
    """
    return run_pipeline_once_from_settings(
        settings=settings,
        source=source,
        content_class=content_class,
        limit=limit,
        ai_limit=ai_limit,
        force=force,
        dry_run=dry_run,
        output_dir=output_dir,
        profile_path=profile_path,
        snapshot_key=snapshot_key,
        ai_client=ai_client,
        fetch_runner=run_intel_fetch_from_settings,
        ai_review_runner=run_ai_review_from_settings,
        event_cluster_runner=run_event_cluster_from_settings,
        editorial_rank_runner=run_editorial_rank_from_settings,
        export_runner=run_intel_export_from_settings,
    )
