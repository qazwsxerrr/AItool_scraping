from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.limits import DEFAULT_FETCH_LIMIT_PER_SOURCE
from app.config.settings import Settings
from app.jobs.fetch_job import run_intel_fetch_from_settings
from app.logging_config import configure_logging


def main(
    limit_per_source: int | None = typer.Option(
        DEFAULT_FETCH_LIMIT_PER_SOURCE,
        min=1,
        help=f"Maximum items to fetch per source (default: {DEFAULT_FETCH_LIMIT_PER_SOURCE}).",
    ),
    source: str | None = typer.Option(None, help="Only fetch one source id, e.g. openai_news."),
    content_class: str | None = typer.Option(None, "--class", help="Only fetch one content class."),
    force: bool = typer.Option(False, "--force", help="Fetch selected sources even when fetch_interval has not elapsed."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch without writing the database."),
) -> None:
    configure_logging()
    result = run_intel_fetch_from_settings(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
        content_class=content_class,
        force=force,
        dry_run=dry_run,
    )

    if result.skipped_sources:
        typer.echo("Skipped configured sources:")
        for skipped in result.skipped_sources:
            typer.echo(f"  - {skipped}")

    if source and source not in result.stats:
        typer.echo(f"No enabled source matched: {source}")
        raise typer.Exit(code=1)

    if result.not_due_sources:
        typer.echo("Not due (use --force to retry):")
        for not_due in result.not_due_sources:
            typer.echo(f"  - {not_due}")

    typer.echo("Fetch stats:")
    for source_id, stats in result.stats.items():
        line = (
            f"  - {source_id}: fetched={stats.fetched} inserted={stats.inserted} "
            f"skipped={stats.skipped} failed={stats.failed} status={stats.status}"
        )
        if stats.error:
            line += f" error={stats.error}"
        typer.echo(line)

    typer.echo(
        f"Totals: fetched={result.total_fetched} inserted={result.total_inserted} "
        f"skipped={result.total_skipped} failed={result.total_failed}"
    )


if __name__ == "__main__":
    typer.run(main)
