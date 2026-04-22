from __future__ import annotations

import typer

from app.config.settings import Settings
from app.jobs.fetch_job import run_fetch_from_registry
from app.logging_config import configure_logging

app = typer.Typer(help="AI tool intelligence ingestion CLI")


@app.command("fetch")
def fetch(
    limit_per_source: int = typer.Option(30, min=1, help="Maximum items to process per source."),
    source: str | None = typer.Option(None, help="Only fetch one source id."),
) -> None:
    configure_logging()
    result = run_fetch_from_registry(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
    )
    for source_id, stats in result.stats.items():
        typer.echo(
            f"{source_id}: fetched={stats.fetched} inserted={stats.inserted} "
            f"skipped={stats.skipped} failed={stats.failed}"
            + (f" error={stats.error}" if stats.error else "")
        )


if __name__ == "__main__":
    app()
