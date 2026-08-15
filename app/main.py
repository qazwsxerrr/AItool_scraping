from __future__ import annotations

import typer

from app.config.limits import (
    DEFAULT_AI_REVIEW_LIMIT,
    DEFAULT_DAILY_REPORT_LIMIT,
    DEFAULT_FETCH_LIMIT_PER_SOURCE,
)
from app.config.settings import Settings
from app.jobs.ai_review_job import run_ai_review_from_settings
from app.jobs.editorial_rank_job import run_editorial_rank_from_settings
from app.jobs.export_job import run_intel_export_from_settings
from app.jobs.fetch_job import run_intel_fetch_from_settings
from app.jobs.fetch_only_job import run_fetch_only_from_settings
from app.jobs.pipeline_orchestrator import (
    adopt_existing_pipeline_from_settings,
    normalize_stage,
    pipeline_status_from_settings,
    resume_pipeline_from_settings,
    retry_pipeline_stage_from_settings,
    run_pipeline_export_from_settings,
    run_pipeline_rank_from_settings,
    run_pipeline_stage_a_from_settings,
    run_pipeline_stage_b_from_settings,
    run_pipeline_stage_c_from_settings,
    start_pipeline_run_from_settings,
)
from app.jobs.run_job import run_intel_once_from_settings
from app.jobs.source_health_job import run_source_health_from_settings
from app.logging_config import configure_logging


app = typer.Typer(help="AI tool intelligence ingestion CLI")
pipeline_app = typer.Typer(help="Run-scoped, resumable intelligence pipeline")
app.add_typer(pipeline_app, name="pipeline")
_CONTENT_CLASSES = {"official_model_company", "project_tool", "community_social"}


@app.command("fetch")
def fetch(
    limit_per_source: int | None = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        "--limit",
        "--limit-per-source",
        min=1,
        help=f"Maximum items to fetch per source (default: {DEFAULT_FETCH_LIMIT_PER_SOURCE}).",
    ),
    source: str | None = typer.Option(None, help="Only fetch one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only fetch one content class."),
    force: bool = typer.Option(False, "--force", help="Ignore fetch_interval cooldown."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch without database writes."),
) -> None:
    configure_logging()
    _validate_content_class(content_class)
    result = run_intel_fetch_from_settings(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
        content_class=content_class,
        force=force,
        dry_run=dry_run,
    )
    if result.not_due_sources:
        typer.echo(f"Not due (use --force to retry): {', '.join(result.not_due_sources)}")
    for source_id, stats in result.stats.items():
        message = (
            f"{source_id}: fetched={stats.fetched} inserted={stats.inserted} "
            f"skipped={stats.skipped} failed={stats.failed} status={stats.status}"
        )
        if stats.error:
            message += f" error={stats.error}"
        typer.echo(message)
    typer.echo(
        f"Totals: fetched={result.total_fetched} inserted={result.total_inserted} "
        f"skipped={result.total_skipped} failed={result.total_failed}"
    )
    if result.skipped_sources:
        typer.echo(f"Registry skipped: {len(result.skipped_sources)}")


@app.command("fetch-only")
def fetch_only(
    limit_per_source: int | None = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        "--limit",
        "--limit-per-source",
        min=1,
        help=f"Maximum items to fetch per source (default: {DEFAULT_FETCH_LIMIT_PER_SOURCE}).",
    ),
    source: str | None = typer.Option(None, help="Only fetch one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only fetch one content class."),
    force: bool = typer.Option(False, "--force", help="Ignore fetch_interval cooldown."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch without database or export writes."),
    output_dir: str = typer.Option("output/fetch", help="Raw/normalized JSON and Markdown output directory."),
) -> None:
    """Run only fetch and export raw/normalized items with source attribution."""

    configure_logging()
    _validate_content_class(content_class)
    result = run_fetch_only_from_settings(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
        content_class=content_class,
        force=force,
        dry_run=dry_run,
        output_dir=output_dir,
    )
    if result.fetch.not_due_sources:
        typer.echo(f"Not due (use --force to retry): {', '.join(result.fetch.not_due_sources)}")
    for source_id, stats in result.fetch.stats.items():
        typer.echo(
            f"{source_id}: fetched={stats.fetched} inserted={stats.inserted} "
            f"skipped={stats.skipped} failed={stats.failed} status={stats.status}"
        )
    typer.echo(
        f"Totals: fetched={result.fetch.total_fetched} inserted={result.fetch.total_inserted} "
        f"skipped={result.fetch.total_skipped} failed={result.fetch.total_failed}"
    )
    if result.fetch.skipped_sources:
        typer.echo(f"Registry skipped: {len(result.fetch.skipped_sources)}")
    if result.export is not None:
        typer.echo(f"exported={result.export.exported} dry_run={result.export.dry_run}")
        typer.echo(f"json={result.export.json_path}")
        typer.echo(f"jsonl={result.export.jsonl_path}")
        typer.echo(f"markdown={result.export.markdown_path}")


@app.command("ai-review")
def ai_review(
    source: str | None = typer.Option(None, "--source", help="Only review one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only review one content class."),
    limit: int = typer.Option(
        DEFAULT_AI_REVIEW_LIMIT,
        min=1,
        help=f"Maximum existing items to review (default: {DEFAULT_AI_REVIEW_LIMIT}).",
    ),
    force: bool = typer.Option(False, "--force", help="Re-review previously handled items."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run selection without AI/database/output writes."),
    output_dir: str = typer.Option("output/ai-review", help="Candidate and audit output directory."),
) -> None:
    """Run AI-only classification and Chinese summary."""

    configure_logging()
    _validate_content_class(content_class)
    result = run_ai_review_from_settings(
        settings=Settings.from_env(),
        source_filter=source,
        content_class=content_class,
        limit=limit,
        force=force,
        dry_run=dry_run,
        output_dir=output_dir,
    )
    typer.echo(
        f"processed={result.processed} selected={result.selected} filtered={result.filtered} "
        f"analyzed={result.analyzed} ai_failed={result.ai_failed} failed={result.failed} "
        f"exported={result.exported} audit={result.audit_exported} dry_run={result.dry_run}"
    )
    typer.echo(f"candidates={result.candidate_path}")
    typer.echo(f"audit={result.audit_path}")
    typer.echo(f"markdown={result.markdown_path}")


@app.command("export")
def export(
    source: str | None = typer.Option(None, help="Only export one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only export one content class."),
    limit: int = typer.Option(
        DEFAULT_DAILY_REPORT_LIMIT,
        min=1,
        help=f"Maximum retained items to export (default: {DEFAULT_DAILY_REPORT_LIMIT}; explicit values override).",
    ),
    output_dir: str = typer.Option("output/intel", help="JSONL/Markdown output directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build records without writing files."),
    snapshot_key: str = typer.Option("latest", "--snapshot", help="Editorial ranking snapshot key."),
) -> None:
    configure_logging()
    _validate_content_class(content_class)
    result = run_intel_export_from_settings(
        settings=Settings.from_env(),
        source_filter=source,
        content_class=content_class,
        limit=limit,
        output_dir=output_dir,
        dry_run=dry_run,
        snapshot_key=snapshot_key,
    )
    typer.echo(f"exported={result.exported} pending={result.pending} dry_run={result.dry_run}")
    typer.echo(f"jsonl={result.jsonl_path}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"pending_jsonl={result.pending_path}")
    if result.github_report_path:
        typer.echo(f"github_report={result.github_report_path}")


@app.command("editorial-rank")
def editorial_rank(
    limit: int | None = typer.Option(None, min=1, help="Maximum events to rank."),
    force: bool = typer.Option(False, "--force", help="Rebuild the snapshot from all candidate events."),
    snapshot_key: str = typer.Option("latest", "--snapshot", help="Ranking snapshot key."),
    profile: str | None = typer.Option(None, "--profile", help="Daily editorial profile YAML path."),
) -> None:
    """Rank aggregated events and enforce the daily editorial quotas."""

    configure_logging()
    result = run_editorial_rank_from_settings(
        settings=Settings.from_env(),
        profile_path=profile,
        limit=limit,
        force=force,
        snapshot_key=snapshot_key,
    )
    typer.echo(
        f"processed={result.processed} selected={result.selected} rejected={result.rejected} "
        f"snapshots={result.snapshots} ai_ranked={result.ai_ranked} ai_failed={result.ai_failed} "
        f"fallback={result.used_fallback} snapshot={result.snapshot_key}"
    )
    for error in result.errors:
        typer.echo(f"error={error}")


@app.command("run-once")
def run_once(
    source: str | None = typer.Option(None, help="Only run one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only run one content class."),
    limit: int = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        "--limit",
        "--fetch-limit",
        min=1,
        help=f"Maximum items to fetch per source (default: {DEFAULT_FETCH_LIMIT_PER_SOURCE}).",
    ),
    ai_limit: int | None = typer.Option(
        None,
        "--ai-limit",
        min=1,
        help="Optional AI safety cap; omitted means no global cap. A supplied cap marks the run partial.",
    ),
    force: bool = typer.Option(False, "--force", help="Ignore cooldown and re-review items."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run without database or export writes."),
    output_dir: str = typer.Option("output/intel", help="JSONL/Markdown output directory."),
    profile: str | None = typer.Option(None, "--profile", help="Daily editorial profile YAML path."),
    snapshot_key: str | None = typer.Option(None, "--snapshot", help="Editorial ranking snapshot key (defaults to run-{run_id})."),
) -> None:
    configure_logging()
    _validate_content_class(content_class)
    result = run_intel_once_from_settings(
        settings=Settings.from_env(),
        source=source,
        content_class=content_class,
        limit=limit,
        ai_limit=ai_limit,
        force=force,
        dry_run=dry_run,
        output_dir=output_dir,
        profile_path=profile,
        snapshot_key=snapshot_key,
    )
    typer.echo(
        f"fetch: fetched={result.fetch.total_fetched} inserted={result.fetch.total_inserted} "
        f"skipped={result.fetch.total_skipped} failed={result.fetch.total_failed}"
    )
    typer.echo(
        f"ai-review: processed={result.ai_review.processed} screened={result.ai_review.screened} "
        f"screened_out={result.ai_review.screened_out} analyzed={result.ai_review.analyzed} "
        f"candidate={result.ai_review.candidate} failed={result.ai_review.failed} partial={result.ai_review.partial}"
    )
    typer.echo(
        f"export: exported={result.export.exported} pending={result.export.pending} "
        f"partial={result.export.partial}"
    )
    if result.event_cluster is not None:
        typer.echo(
            f"event-cluster: processed={result.event_cluster.processed} events={result.event_cluster.events} "
            f"failed={result.event_cluster.failed}"
        )
    if result.editorial_rank is not None:
        typer.echo(
            f"editorial-rank: selected={result.editorial_rank.selected} rejected={result.editorial_rank.rejected} "
            f"ai_failed={result.editorial_rank.ai_failed}"
        )
    if result.export.github_report_path:
        typer.echo(f"github_report={result.export.github_report_path}")
    typer.echo(f"run_id={result.run_id} status={result.status}")
    if result.error:
        typer.echo(f"error={result.error}")
    if result.status == "failed":
        raise typer.Exit(code=1)


@pipeline_app.command("start")
def pipeline_start(
    source: str | None = typer.Option(None, "--source", help="Freeze one source id into the new run."),
    content_class: str | None = typer.Option(None, "--class", help="Freeze one content class into the new run."),
    limit: int | None = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        "--limit",
        min=1,
        help="Maximum items fetched per source for this run.",
    ),
    force: bool = typer.Option(False, "--force", help="Ignore source cooldown for this fetch stage only."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Diagnostic fetch without creating a durable run."),
) -> None:
    """Create and freeze a run scope, then perform fetch only."""

    configure_logging()
    _validate_content_class(content_class)
    result = start_pipeline_run_from_settings(
        settings=Settings.from_env(),
        source=source,
        content_class=content_class,
        limit=limit,
        force=force,
        dry_run=dry_run,
    )
    typer.echo(f"run_id={result.run_id} scope_frozen={result.scope_frozen}")
    typer.echo(
        f"fetch: fetched={result.fetch.total_fetched} inserted={result.fetch.total_inserted} "
        f"skipped={result.fetch.total_skipped} failed={result.fetch.total_failed}"
    )
    if result.reference_time is not None:
        typer.echo(f"reference_time={result.reference_time.isoformat()}")


@pipeline_app.command("stage-a")
def pipeline_stage_a(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing frozen pipeline run id."),
    source: str | None = typer.Option(None, "--source"),
    content_class: str | None = typer.Option(None, "--class"),
    limit: int | None = typer.Option(DEFAULT_AI_REVIEW_LIMIT, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Re-run Stage A tasks only."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Retry failed Stage A tasks only."),
    include_blocked: bool = typer.Option(False, "--include-blocked", help="Include blocked Stage A tasks."),
) -> None:
    """Run only Stage A (lightweight screening) for an existing run."""

    configure_logging()
    _validate_content_class(content_class)
    result = run_pipeline_stage_a_from_settings(
        settings=Settings.from_env(),
        run_id=run_id,
        source=source,
        content_class=content_class,
        limit=limit,
        force=force,
        retry_failed=retry_failed,
        include_blocked=include_blocked,
    )
    typer.echo(
        f"run_id={run_id} stage-a: processed={result.processed} screened={result.screened} "
        f"screened_out={result.screened_out} failed={result.screen_failed} skipped={result.skipped}"
    )


@pipeline_app.command("stage-b")
def pipeline_stage_b(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing frozen pipeline run id."),
    source: str | None = typer.Option(None, "--source"),
    content_class: str | None = typer.Option(None, "--class"),
    limit: int | None = typer.Option(DEFAULT_AI_REVIEW_LIMIT, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Re-run Stage B tasks only."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Retry failed Stage B tasks only."),
    include_blocked: bool = typer.Option(False, "--include-blocked", help="Include blocked Stage B tasks."),
) -> None:
    """Run only Stage B (full analysis) for Stage-A eligible items."""

    configure_logging()
    _validate_content_class(content_class)
    result = run_pipeline_stage_b_from_settings(
        settings=Settings.from_env(),
        run_id=run_id,
        source=source,
        content_class=content_class,
        limit=limit,
        force=force,
        retry_failed=retry_failed,
        include_blocked=include_blocked,
    )
    typer.echo(
        f"run_id={run_id} stage-b: processed={result.processed} analyzed={result.analyzed} "
        f"filtered={result.analysis_filtered} candidate={result.candidate} "
        f"failed={result.analysis_failed} skipped={result.skipped}"
    )


@pipeline_app.command("stage-c")
def pipeline_stage_c(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing frozen pipeline run id."),
    limit: int | None = typer.Option(DEFAULT_AI_REVIEW_LIMIT, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Re-run Stage C only."),
    snapshot_key: str | None = typer.Option(None, "--snapshot", help="Run-scoped snapshot key override."),
) -> None:
    """Run only Stage C event clustering with the frozen reference time."""

    configure_logging()
    result = run_pipeline_stage_c_from_settings(
        settings=Settings.from_env(),
        run_id=run_id,
        limit=limit,
        force=force,
        snapshot_key=snapshot_key,
    )
    typer.echo(
        f"run_id={run_id} stage-c: processed={result.processed} events={result.events} "
        f"repeats={result.repeats} failed={result.failed} snapshot={result.snapshot_key}"
    )


@pipeline_app.command("rank")
def pipeline_rank(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing frozen pipeline run id."),
    limit: int | None = typer.Option(None, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Rebuild this run's ranking snapshot only."),
    snapshot_key: str | None = typer.Option(None, "--snapshot", help="Run-scoped snapshot key override."),
    profile: str | None = typer.Option(None, "--profile", help="Daily editorial profile YAML path."),
) -> None:
    """Rank Stage-C events for one run."""

    configure_logging()
    result = run_pipeline_rank_from_settings(
        settings=Settings.from_env(),
        run_id=run_id,
        limit=limit,
        force=force,
        snapshot_key=snapshot_key,
        profile_path=profile,
    )
    typer.echo(
        f"run_id={run_id} rank: processed={result.processed} selected={result.selected} "
        f"rejected={result.rejected} failed={result.ai_failed} snapshot={result.snapshot_key}"
    )


@pipeline_app.command("export")
def pipeline_export(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing frozen pipeline run id."),
    source: str | None = typer.Option(None, "--source"),
    content_class: str | None = typer.Option(None, "--class"),
    limit: int = typer.Option(DEFAULT_DAILY_REPORT_LIMIT, "--limit", min=1),
    output_dir: str = typer.Option("output/intel", "--output-dir"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    snapshot_key: str | None = typer.Option(None, "--snapshot", help="Run-scoped snapshot key override."),
) -> None:
    """Export one run's ranked events and pending audit records."""

    configure_logging()
    _validate_content_class(content_class)
    result = run_pipeline_export_from_settings(
        settings=Settings.from_env(),
        run_id=run_id,
        source=source,
        content_class=content_class,
        limit=limit,
        output_dir=output_dir,
        dry_run=dry_run,
        snapshot_key=snapshot_key,
    )
    typer.echo(
        f"run_id={run_id} export: exported={result.exported} pending={result.pending} "
        f"partial={result.partial} snapshot={result.snapshot_key}"
    )
    typer.echo(f"jsonl={result.jsonl_path}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"pending_jsonl={result.pending_path}")


@pipeline_app.command("status")
def pipeline_status(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing pipeline run id."),
) -> None:
    """Show every stage's status and fail/block counts for one run."""

    configure_logging()
    status = pipeline_status_from_settings(settings=Settings.from_env(), run_id=run_id)
    typer.echo(
        f"run_id={status.run_id} status={status.run_status} scope_frozen={status.scope_frozen} "
        f"reference_time={status.reference_time.isoformat() if status.reference_time else '-'}"
    )
    for row in status.stages:
        typer.echo(
            f"{row['stage']}: status={row['status']} total={row['total']} "
            f"pending={row['pending']} running={row['running']} succeeded={row['succeeded']} "
            f"failed={row['failed']} retry_waiting={row['retry_waiting']} blocked={row['blocked']}"
        )
    typer.echo(f"failures={status.total_failures} blocked={status.total_blocked}")


@pipeline_app.command("retry")
def pipeline_retry(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing pipeline run id."),
    stage: str = typer.Option(..., "--stage", help="One stage: stage-a, stage-b, stage-c, rank, export."),
    source: str | None = typer.Option(None, "--source"),
    content_class: str | None = typer.Option(None, "--class"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Force only the named stage."),
    include_blocked: bool = typer.Option(False, "--include-blocked"),
    output_dir: str = typer.Option("output/intel", "--output-dir"),
    snapshot_key: str | None = typer.Option(None, "--snapshot"),
    profile: str | None = typer.Option(None, "--profile"),
) -> None:
    """Retry failed/retry-waiting tasks in exactly one named stage."""

    configure_logging()
    _validate_content_class(content_class)
    try:
        canonical = normalize_stage(stage)
        result = retry_pipeline_stage_from_settings(
            settings=Settings.from_env(),
            run_id=run_id,
            stage=canonical,
            include_blocked=include_blocked,
            force=force,
            source=source,
            content_class=content_class,
            limit=limit,
            output_dir=output_dir,
            snapshot_key=snapshot_key,
            profile_path=profile,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--stage") from exc
    if result is None:
        typer.echo(f"run_id={run_id} stage={canonical} retryable=0")
        return
    typer.echo(f"run_id={run_id} stage={canonical} retried=true")
    if getattr(result, "errors", None):
        for error in result.errors:
            typer.echo(f"error={error}")


@pipeline_app.command("resume")
def pipeline_resume(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing frozen pipeline run id."),
    fetch: bool = typer.Option(False, "--fetch", help="Explicitly attempt fetch for this existing run."),
    source: str | None = typer.Option(None, "--source"),
    content_class: str | None = typer.Option(None, "--class"),
    limit: int | None = typer.Option(None, "--limit", min=1),
    output_dir: str = typer.Option("output/intel", "--output-dir"),
    snapshot_key: str | None = typer.Option(None, "--snapshot"),
    profile: str | None = typer.Option(None, "--profile"),
) -> None:
    """Resume pending/retryable stages in dependency order without fetching."""

    configure_logging()
    _validate_content_class(content_class)
    result = resume_pipeline_from_settings(
        settings=Settings.from_env(),
        run_id=run_id,
        fetch=fetch,
        source=source,
        content_class=content_class,
        limit=limit,
        output_dir=output_dir,
        snapshot_key=snapshot_key,
        profile_path=profile,
    )
    typer.echo(f"run_id={run_id} resumed={','.join(result.ran_stages) or '-'}")
    if result.skipped_stages:
        typer.echo(f"skipped={','.join(result.skipped_stages)}")
    for error in result.errors:
        typer.echo(f"error={error}")
    if result.errors:
        raise typer.Exit(code=1)


@pipeline_app.command("adopt-existing")
def pipeline_adopt_existing(
    run_id: int = typer.Option(..., "--run-id", min=1, help="Existing run id to reconstruct."),
) -> None:
    """Adopt matching current Stage-A/B projections without any AI calls."""

    configure_logging()
    result = adopt_existing_pipeline_from_settings(settings=Settings.from_env(), run_id=run_id)
    typer.echo(
        f"run_id={run_id} adopted_stage_a={result.adopted.get('screen', 0)} "
        f"adopted_stage_b={result.adopted.get('analyze', 0)}"
    )


@app.command("source-health")
def source_health(source: str | None = typer.Option(None, "--source")) -> None:
    configure_logging()
    for row in run_source_health_from_settings(settings=Settings.from_env(), source_filter=source):
        next_time = row.next_fetch_at.isoformat() if row.next_fetch_at else "now"
        typer.echo(f"{row.source_id}: status={row.status} failures={row.consecutive_failures} next={next_time} error={row.error_code or '-'}")


def _validate_content_class(value: str | None) -> None:
    if value is not None and value not in _CONTENT_CLASSES:
        raise typer.BadParameter("--class must be official_model_company, project_tool, or community_social")


if __name__ == "__main__":
    app()
