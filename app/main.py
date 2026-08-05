from __future__ import annotations

import typer

from app.config.settings import Settings
from app.jobs.export_job import run_intel_export_from_settings
from app.jobs.fetch_job import run_intel_fetch_from_settings
from app.jobs.process_job import run_intel_process_from_settings
from app.jobs.run_job import run_intel_once_from_settings
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


@app.command("process")
def process(
    source: str | None = typer.Option(None, help="Only process one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only process one content class."),
    limit: int = typer.Option(100, min=1, help="Maximum items to process."),
    force: bool = typer.Option(False, "--force", help="Reprocess existing items."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run without AI or database writes."),
) -> None:
    configure_logging()
    _validate_content_class(content_class)
    result = run_intel_process_from_settings(
        settings=Settings.from_env(),
        source_filter=source,
        content_class=content_class,
        limit=limit,
        force=force,
        dry_run=dry_run,
    )
    typer.echo(
        f"processed={result.processed} selected={result.selected} filtered={result.filtered} "
        f"analyzed={result.analyzed} verified={result.verified} needs_review={result.needs_review} "
        f"ai_failed={result.ai_failed} failed={result.failed}"
    )


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


@app.command("run-once")
def run_once(
    source: str | None = typer.Option(None, help="Only run one source id."),
    content_class: str | None = typer.Option(None, "--class", help="Only run one content class."),
    limit: int = typer.Option(100, min=1, help="Per-stage item limit."),
    force: bool = typer.Option(False, "--force", help="Ignore cooldown and reprocess items."),
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
        f"process: processed={result.process.processed} selected={result.process.selected} "
        f"analyzed={result.process.analyzed} failed={result.process.failed}"
    )
    typer.echo(f"export: exported={result.export.exported} pending={result.export.pending}")
    typer.echo(f"run_id={result.run_id} status={result.status}")
    if result.error:
        typer.echo(f"error={result.error}")
    if result.status == "failed":
        raise typer.Exit(code=1)


def _validate_content_class(value: str | None) -> None:
    if value is not None and value not in _CONTENT_CLASSES:
        raise typer.BadParameter("--class must be official_model_company, project_tool, or community_social")


if __name__ == "__main__":
    app()
