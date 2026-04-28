from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.fetch_job import run_fetch_from_registry
from app.logging_config import configure_logging


def main(
    limit_per_source: int | None = typer.Option(
        None,
        min=1,
        help="Maximum items to process per source. Defaults to each source's configured default_limit.",
    ),
    source: str | None = typer.Option(None, help="Only fetch one source id, e.g. openai_news."),
    group: str | None = typer.Option(None, help="Only fetch one source group, e.g. reddit_local_llama."),
) -> None:
    configure_logging()
    result = run_fetch_from_registry(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
        source_group_filter=group,
    )

    if result.skipped_sources:
        typer.echo("Skipped configured sources:")
        for skipped in result.skipped_sources:
            typer.echo(f"  - {skipped}")

    if source and source not in result.stats:
        typer.echo(f"No enabled source matched: {source}")
        raise typer.Exit(code=1)

    if group and not result.stats:
        typer.echo(f"No enabled source group matched: {group}")
        raise typer.Exit(code=1)

    typer.echo("Fetch stats:")
    for source_id, stats in result.stats.items():
        line = (
            f"  - {source_id}: fetched={stats.fetched} inserted={stats.inserted} "
            f"skipped={stats.skipped} failed={stats.failed}"
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
