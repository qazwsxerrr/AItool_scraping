from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from app.config.limits import (
    DEFAULT_AI_REVIEW_LIMIT,
    DEFAULT_DAILY_REPORT_LIMIT,
    DEFAULT_FETCH_LIMIT_PER_SOURCE,
)
from app.config.settings import Settings
from app.jobs.fetch_job import run_intel_fetch_from_settings
from app.jobs.fetch_only_job import run_fetch_only_from_settings
from app.jobs.pipeline_orchestrator import (
    normalize_stage,
    publish_daily_draft_from_settings,
    pipeline_edition_status_from_settings,
    resolve_pending_daily_draft_from_settings,
    resume_pipeline_from_settings,
    retry_pipeline_stage_from_settings,
    run_pipeline_from_settings,
    run_pipeline_stage_d_from_settings,
    run_pipeline_stage_a_from_settings,
    run_pipeline_stage_b_from_settings,
    run_pipeline_stage_c_from_settings,
    start_pipeline_run_from_settings,
)
from app.jobs.source_health_job import run_source_health_from_settings
from app.logging_config import configure_logging
from app.storage.draft_workspace import audit_database_path


app = typer.Typer(help="AI tool intelligence ingestion CLI")
pipeline_app = typer.Typer(help="Date-addressed, resumable daily intelligence pipeline")
app.add_typer(pipeline_app, name="pipeline")
_CONTENT_CLASSES = {"official_model_company", "project_tool", "community_social", "news_media"}


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
) -> None:
    configure_logging()
    _validate_content_class(content_class)
    result = run_intel_fetch_from_settings(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
        content_class=content_class,
        force=force,
        dry_run=True,
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
    typer.echo(f"exported={result.export.exported}")
    typer.echo(f"json={result.export.json_path}")
    typer.echo(f"jsonl={result.export.jsonl_path}")
    typer.echo(f"markdown={result.export.markdown_path}")


@app.command("run-once")
def run_once(
    limit: int = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        "--limit",
        "--fetch-limit",
        min=1,
        help=f"Maximum items to fetch per source (default: {DEFAULT_FETCH_LIMIT_PER_SOURCE}).",
    ),
    edition_date: str | None = typer.Option(
        None,
        "--edition-date",
        metavar="YYYY-MM-DD",
        help="Public daily edition to update; defaults to the current Asia/Shanghai date.",
    ),
    output_dir: str = typer.Option("output/intel", help="JSONL/Markdown output directory."),
    profile: str | None = typer.Option(None, "--profile", help="Daily editorial profile YAML path."),
    publish: bool = typer.Option(False, "--publish", help="Approve and replace the public daily report after a complete draft."),
) -> None:
    configure_logging()
    result = run_pipeline_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        edition_date=_optional_edition_date(edition_date),
        output_dir=output_dir,
        profile_path=profile,
        publish=publish,
    )
    typer.echo(
        f"fetch: fetched={result.start.fetch.total_fetched} inserted={result.start.fetch.total_inserted} "
        f"skipped={result.start.fetch.total_skipped} failed={result.start.fetch.total_failed}"
    )
    exported = result.resume.results.get("export")
    if exported is not None:
        typer.echo(f"export: exported={exported.exported}")
        _echo_daily_edition(exported.markdown_path)
    typer.echo(f"status={result.status}")
    if result.status == "ready_for_publish" and result.start.edition_date:
        typer.echo(f"next=pipeline export --edition-date {result.start.edition_date}")
    if result.status in {"failed", "partial", "draft_failed"}:
        for error in result.resume.errors:
            typer.echo(f"error={error}")
        raise typer.Exit(code=1)


@pipeline_app.command("start")
def pipeline_start(
    limit: int | None = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        "--limit",
        min=1,
        help="Maximum items fetched per source for this run.",
    ),
    edition_date: str | None = typer.Option(
        None,
        "--edition-date",
        metavar="YYYY-MM-DD",
        help="Public daily edition to update; defaults to the current Asia/Shanghai date.",
    ),
) -> None:
    """Create one complete daily draft and perform its fetch stage."""

    configure_logging()
    result = start_pipeline_run_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        edition_date=_optional_edition_date(edition_date),
    )
    if result.edition_date:
        typer.echo(f"edition_date={result.edition_date}")
    typer.echo(f"scope_frozen={result.scope_frozen}")
    typer.echo(
        f"fetch: fetched={result.fetch.total_fetched} inserted={result.fetch.total_inserted} "
        f"skipped={result.fetch.total_skipped} failed={result.fetch.total_failed}"
    )
    if result.reference_time is not None:
        typer.echo(f"reference_time={result.reference_time.isoformat()}")


@pipeline_app.command("run")
def pipeline_run(
    limit: int | None = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        "--limit",
        min=1,
        help="Maximum items fetched per source for this run.",
    ),
    output_dir: str = typer.Option("output/intel", "--output-dir"),
    profile: str | None = typer.Option(None, "--profile", help="Daily editorial profile YAML path."),
    edition_date: str | None = typer.Option(
        None,
        "--edition-date",
        metavar="YYYY-MM-DD",
        help="Public daily edition to update; defaults to the current Asia/Shanghai date.",
    ),
    publish: bool = typer.Option(False, "--publish", help="Approve and replace the public daily report after a complete draft."),
) -> None:
    """Run A-D into a private draft; use --publish only after approval."""

    configure_logging()

    def announce_start(start) -> None:
        typer.echo(f"scope_frozen={start.scope_frozen}")
        if start.edition_date:
            typer.echo(f"edition_date={start.edition_date}")

    result = run_pipeline_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        edition_date=_optional_edition_date(edition_date),
        output_dir=output_dir,
        profile_path=profile,
        on_start=announce_start,
        publish=publish,
    )
    typer.echo(f"status={result.status}")
    export_result = result.resume.results.get("export")
    if export_result is not None:
        _echo_daily_edition(getattr(export_result, "markdown_path", None))
    typer.echo(
        f"fetch: fetched={result.start.fetch.total_fetched} "
        f"inserted={result.start.fetch.total_inserted} "
        f"skipped={result.start.fetch.total_skipped} "
        f"failed={result.start.fetch.total_failed}"
    )
    typer.echo(f"stages={','.join(result.resume.ran_stages) or '-'}")
    if result.resume.skipped_stages:
        typer.echo(f"skipped={','.join(result.resume.skipped_stages)}")
    for error in result.resume.errors:
        typer.echo(f"error={error}")
    if result.status == "ready_for_publish" and result.start.edition_date:
        typer.echo(f"next=pipeline export --edition-date {result.start.edition_date}")
    if result.status in {"failed", "partial", "draft_failed"} or result.resume.errors:
        raise typer.Exit(code=1)


@pipeline_app.command("stage-a")
def pipeline_stage_a(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to process."),
    limit: int | None = typer.Option(DEFAULT_AI_REVIEW_LIMIT, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Re-run Stage A tasks only."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Retry failed Stage A tasks only."),
    include_blocked: bool = typer.Option(False, "--include-blocked", help="Include blocked Stage A tasks."),
) -> None:
    """Run only Stage A (lightweight screening) for the current daily draft."""

    configure_logging()
    settings = Settings.from_env()
    workspace_settings, run_id = _resolve_pending_draft(settings, edition_date)
    result = run_pipeline_stage_a_from_settings(
        settings=workspace_settings,
        run_id=run_id,
        limit=limit,
        force=force,
        retry_failed=retry_failed,
        include_blocked=include_blocked,
    )
    typer.echo(
        f"stage-a: processed={result.processed} screened={result.screened} "
        f"screened_out={result.screened_out} failed={result.screen_failed} skipped={result.skipped}"
    )


@pipeline_app.command("stage-b1")
def pipeline_stage_b1(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to process."),
    limit: int | None = typer.Option(DEFAULT_AI_REVIEW_LIMIT, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Re-run Stage B tasks only."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Retry failed Stage B tasks only."),
    include_blocked: bool = typer.Option(False, "--include-blocked", help="Include blocked Stage B tasks."),
) -> None:
    """Run Stage B1 content-value analysis for the current daily draft."""

    configure_logging()
    settings = Settings.from_env()
    workspace_settings, run_id = _resolve_pending_draft(settings, edition_date)
    result = run_pipeline_stage_b_from_settings(
        settings=workspace_settings,
        run_id=run_id,
        limit=limit,
        force=force,
        retry_failed=retry_failed,
        include_blocked=include_blocked,
    )
    typer.echo(
        f"stage-b1: processed={result.processed} analyzed={result.analyzed} "
        f"structurally_filtered={result.analysis_filtered} "
        f"active={result.candidate} reserve={result.reserve} target={result.admission_target or 0} "
        f"failed={result.analysis_failed} skipped={result.skipped}"
    )


@pipeline_app.command("stage-c")
def pipeline_stage_c(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to process."),
    force: bool = typer.Option(False, "--force", help="Re-run Stage C only."),
) -> None:
    """Run the stateful Stage C event-aggregation agent for the current draft."""

    configure_logging()
    settings = Settings.from_env()
    workspace_settings, run_id = _resolve_pending_draft(settings, edition_date)
    result = run_pipeline_stage_c_from_settings(
        settings=workspace_settings,
        run_id=run_id,
        force=force,
    )
    typer.echo(
        f"stage-c: processed={result.processed} events={result.events} "
        f"merged={result.merged} repeats={result.repeats} updated={result.updated} "
        f"needs_review={result.unresolved} turns={result.turns} tools={result.tool_calls} web={result.web_searches}"
    )


@pipeline_app.command("stage-d")
def pipeline_stage_d(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to process."),
    force: bool = typer.Option(False, "--force", help="Rebuild this daily draft's Stage-D selection only."),
    profile: str | None = typer.Option(None, "--profile", help="Daily editorial profile YAML path."),
) -> None:
    """Run final editorial selection for one Stage-C event pool."""

    configure_logging()
    settings = Settings.from_env()
    workspace_settings, run_id = _resolve_pending_draft(settings, edition_date)
    result = run_pipeline_stage_d_from_settings(
        settings=workspace_settings,
        run_id=run_id,
        force=force,
        profile_path=profile,
    )
    typer.echo(
        f"stage-d: candidates={result.candidates} selected={result.selected} "
        f"unselected={result.unselected} reused={result.reused} failed={result.ai_failed}"
    )


@pipeline_app.command("export")
def pipeline_export(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to export."),
    limit: int = typer.Option(DEFAULT_DAILY_REPORT_LIMIT, "--limit", min=1),
    output_dir: str = typer.Option("output/intel", "--output-dir"),
) -> None:
    """Publish the current daily draft's selected Stage-D events."""

    configure_logging()
    settings = Settings.from_env()
    result = publish_daily_draft_from_settings(
        settings=settings,
        edition_date=_required_edition_date(edition_date),
        limit=limit,
        output_dir=output_dir,
    )
    typer.echo(
        f"export: exported={result.exported} partial={result.partial}"
    )
    _echo_daily_edition(result.markdown_path)
    typer.echo(f"jsonl={result.jsonl_path}")
    typer.echo(f"markdown={result.markdown_path}")
    if result.manifest_path:
        typer.echo(f"manifest={result.manifest_path}")
    typer.echo(
        f"audit={audit_database_path(settings.database_url, _required_edition_date(edition_date))}"
    )


@pipeline_app.command("status")
def pipeline_status(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to inspect."),
) -> None:
    """Show public, draft, and retained-audit state for one daily report."""

    configure_logging()
    settings = Settings.from_env()
    normalized = _required_edition_date(edition_date)
    try:
        status = pipeline_edition_status_from_settings(settings=settings, edition_date=normalized)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--edition-date") from exc
    typer.echo(f"edition_date={status.edition_date}")
    typer.echo(
        f"status={status.status} draft_status={status.draft_status or '-'} "
        f"audit_status={status.audit_status or '-'} "
        f"published_at={status.published_at.isoformat() if status.published_at else '-'}"
    )
    if status.audit_path:
        typer.echo(f"audit={status.audit_path}")
    for row in status.stages:
        typer.echo(
            f"{row['stage']}: status={row['status']} total={row['total']} "
            f"pending={row['pending']} running={row['running']} succeeded={row['succeeded']} "
            f"failed={row['failed']} retry_waiting={row['retry_waiting']} blocked={row['blocked']}"
        )
    typer.echo(f"failures={status.total_failures} blocked={status.total_blocked}")


@pipeline_app.command("retry")
def pipeline_retry(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to retry."),
    stage: str = typer.Option(..., "--stage", help="One stage: stage-a, stage-b1, stage-c, or stage-d."),
    limit: int | None = typer.Option(None, "--limit", min=1),
    force: bool = typer.Option(False, "--force", help="Force only the named stage."),
    include_blocked: bool = typer.Option(False, "--include-blocked"),
    output_dir: str = typer.Option("output/intel", "--output-dir"),
    profile: str | None = typer.Option(None, "--profile"),
) -> None:
    """Retry failed/retry-waiting tasks in exactly one named stage."""

    configure_logging()
    settings = Settings.from_env()
    workspace_settings, run_id = _resolve_pending_draft(settings, edition_date)
    try:
        canonical = normalize_stage(stage)
        result = retry_pipeline_stage_from_settings(
            settings=workspace_settings,
            run_id=run_id,
            stage=canonical,
            include_blocked=include_blocked,
            force=force,
            limit=limit,
            output_dir=output_dir,
            profile_path=profile,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--stage") from exc
    if result is None:
        typer.echo(f"stage={canonical} retryable=0")
        return
    typer.echo(f"stage={canonical} retried=true")
    if getattr(result, "errors", None):
        for error in result.errors:
            typer.echo(f"error={error}")


@pipeline_app.command("resume")
def pipeline_resume(
    edition_date: str = typer.Option(..., "--edition-date", metavar="YYYY-MM-DD", help="Daily edition to resume."),
    limit: int | None = typer.Option(None, "--limit", min=1),
    output_dir: str = typer.Option("output/intel", "--output-dir"),
    profile: str | None = typer.Option(None, "--profile"),
) -> None:
    """Resume pending/retryable stages for the current daily draft."""

    configure_logging()
    settings = Settings.from_env()
    workspace_settings, run_id = _resolve_pending_draft(settings, edition_date)
    result = resume_pipeline_from_settings(
        settings=workspace_settings,
        run_id=run_id,
        limit=limit,
        output_dir=output_dir,
        profile_path=profile,
    )
    typer.echo(f"resumed={','.join(result.ran_stages) or '-'}")
    if result.skipped_stages:
        typer.echo(f"skipped={','.join(result.skipped_stages)}")
    for error in result.errors:
        typer.echo(f"error={error}")
    if result.errors:
        raise typer.Exit(code=1)

@app.command("source-health")
def source_health(source: str | None = typer.Option(None, "--source")) -> None:
    configure_logging()
    for row in run_source_health_from_settings(settings=Settings.from_env(), source_filter=source):
        next_time = row.next_fetch_at.isoformat() if row.next_fetch_at else "now"
        typer.echo(f"{row.source_id}: status={row.status} failures={row.consecutive_failures} next={next_time} error={row.error_code or '-'}")


def _validate_content_class(value: str | None) -> None:
    if value is not None and value not in _CONTENT_CLASSES:
        raise typer.BadParameter(
            "--class must be official_model_company, project_tool, community_social, or news_media"
        )


def _optional_edition_date(value: str | None) -> str | None:
    if value is None:
        return None
    return _required_edition_date(value)


def _required_edition_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter("must use YYYY-MM-DD", param_hint="--edition-date") from exc


def _resolve_pending_draft(settings: Settings, edition_date: str) -> tuple[Settings, int]:
    normalized = _required_edition_date(edition_date)
    try:
        return resolve_pending_daily_draft_from_settings(settings=settings, edition_date=normalized)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--edition-date") from exc


def _echo_daily_edition(markdown_path: str | None) -> None:
    """Print the public daily identifier instead of an internal run ID."""

    if not markdown_path:
        return
    try:
        edition_date = date.fromisoformat(Path(markdown_path).parent.name).isoformat()
    except (TypeError, ValueError):
        return
    typer.echo(f"edition_date={edition_date}")


if __name__ == "__main__":
    app()
