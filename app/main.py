from __future__ import annotations

import typer

from app.config.settings import Settings
from app.jobs.ai_review_job import run_ai_review_from_settings
from app.jobs.export_job import run_intel_export_from_settings
from app.jobs.fetch_job import run_intel_fetch_from_settings
from app.jobs.fetch_only_job import run_fetch_only_from_settings
from app.jobs.run_job import run_intel_once_from_settings
from app.jobs.source_health_job import run_source_health_from_settings
from app.logging_config import configure_logging


app = typer.Typer(help="AI tool intelligence ingestion CLI")
_CONTENT_CLASSES = {"official_model_company", "project_tool", "community_social"}


@app.command("fetch")
def fetch(
    limit_per_source: int | None = typer.Option(
        None,
        "--limit",
        "--limit-per-source",
        min=1,
        help="Maximum items to fetch per source.",
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
        None,
        "--limit",
        "--limit-per-source",
        min=1,
        help="Maximum items to fetch per source.",
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
    limit: int = typer.Option(100, min=1, help="Maximum items to review."),
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
    limit: int = typer.Option(100, min=1, help="Maximum retained items to export."),
    output_dir: str = typer.Option("output/intel", help="JSONL/Markdown output directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build records without writing files."),
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
    )
    typer.echo(f"exported={result.exported} pending={result.pending} dry_run={result.dry_run}")
    typer.echo(f"jsonl={result.jsonl_path}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"pending_jsonl={result.pending_path}")
    if result.github_report_path:
        typer.echo(f"github_report={result.github_report_path}")


@app.command("run-once")
def run_once(
    source: str | None = typer.Option(None, help="Only run one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only run one content class."),
    limit: int = typer.Option(100, min=1, help="Per-stage item limit."),
    force: bool = typer.Option(False, "--force", help="Ignore cooldown and re-review items."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run without database or export writes."),
    output_dir: str = typer.Option("output/intel", help="JSONL/Markdown output directory."),
) -> None:
    configure_logging()
    _validate_content_class(content_class)
    result = run_intel_once_from_settings(
        settings=Settings.from_env(),
        source=source,
        content_class=content_class,
        limit=limit,
        force=force,
        dry_run=dry_run,
        output_dir=output_dir,
    )
    typer.echo(
        f"fetch: fetched={result.fetch.total_fetched} inserted={result.fetch.total_inserted} "
        f"skipped={result.fetch.total_skipped} failed={result.fetch.total_failed}"
    )
    typer.echo(
        f"ai-review: processed={result.ai_review.processed} selected={result.ai_review.selected} "
        f"analyzed={result.ai_review.analyzed} ai_failed={result.ai_review.ai_failed} failed={result.ai_review.failed}"
    )
    typer.echo(f"export: exported={result.export.exported} pending={result.export.pending}")
    if result.export.github_report_path:
        typer.echo(f"github_report={result.export.github_report_path}")
    typer.echo(f"run_id={result.run_id} status={result.status}")
    if result.error:
        typer.echo(f"error={result.error}")
    if result.status == "failed":
        raise typer.Exit(code=1)


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
