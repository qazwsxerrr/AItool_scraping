"""Compatibility facade for the historical ``run-once`` command.

The durable control-plane implementation lives in :mod:`pipeline_orchestrator`.
This module keeps the old import and monkeypatch seams used by integrations and
tests, then delegates the actual run to that orchestrator.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.config.limits import (
    DEFAULT_FETCH_LIMIT_PER_SOURCE,
)
from app.config.settings import Settings
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
    edition_date: date | str | None = None,
    output_dir: str = "output/intel",
    profile_path: str | Path | None = None,
    snapshot_key: str | None = None,
    ai_client: object | None = None,
) -> IntelRunResult:
    """Run the complete convenience pipeline through the durable orchestrator.

    The public command now shares the exact daily-build workflow used by
    ``pipeline run``: a fresh all-source build, publish-on-success, then
    deletion of its temporary working rows.
    """
    return run_pipeline_once_from_settings(
        settings=settings,
        source=source,
        content_class=content_class,
        limit=limit,
        ai_limit=ai_limit,
        force=force,
        dry_run=dry_run,
        edition_date=edition_date,
        output_dir=output_dir,
        profile_path=profile_path,
        snapshot_key=snapshot_key,
        ai_client=ai_client,
    )
