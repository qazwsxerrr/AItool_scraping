"""Fixed-order V3 daily orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.settings import Settings
from app.jobs.cluster_job import ClusterResult, run_cluster_from_settings
from app.jobs.compose_job import ComposeResult, run_compose_from_settings
from app.jobs.daily_export_job import DailyExportResult, run_daily_export_from_settings
from app.jobs.enrich_job import EnrichResult, run_enrich_from_settings
from app.jobs.fetch_job import IntelFetchResult, run_intel_fetch_from_settings
from app.jobs.triage_job import TriageJobResult, run_triage_from_settings


@dataclass
class DailyRunResult:
    fetch: IntelFetchResult
    enrich: EnrichResult
    triage: TriageJobResult
    cluster: ClusterResult
    compose: ComposeResult
    export: DailyExportResult
    status: str


def run_daily_from_settings(*, settings: Settings, source: str | None = None, limit: int = 100, force: bool = False, output_dir: str = "output/daily", dry_run: bool = False, edition_date: str | None = None) -> DailyRunResult:
    fetch = run_intel_fetch_from_settings(settings=settings, source_filter=source, limit_per_source=limit, force=force, dry_run=dry_run)
    enrich = run_enrich_from_settings(settings=settings, source_filter=source, limit=limit, force=force)
    triage = run_triage_from_settings(settings=settings, source_filter=source, limit=limit, force=force)
    cluster = run_cluster_from_settings(settings=settings, limit=limit, force=force)
    compose = run_compose_from_settings(settings=settings, limit=limit, force=force)
    export = run_daily_export_from_settings(settings=settings, edition_date=edition_date, output_dir=output_dir, force=force)
    status = "completed" if export.published and not fetch.total_failed and not triage.failed and not compose.failed else "completed_with_errors"
    return DailyRunResult(fetch, enrich, triage, cluster, compose, export, status)


__all__ = ["DailyRunResult", "run_daily_from_settings"]
