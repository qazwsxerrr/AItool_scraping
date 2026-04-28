from __future__ import annotations

import typer

from app.config.settings import Settings
from app.jobs.fetch_job import run_fetch_from_registry
from app.jobs.normalize_job import run_normalize_from_settings
from app.jobs.prefilter_job import run_prefilter_from_settings
from app.logging_config import configure_logging

app = typer.Typer(help="AI tool intelligence ingestion CLI")


@app.command("fetch")
def fetch(
    limit_per_source: int | None = typer.Option(
        None,
        min=1,
        help="Maximum items to process per source. Defaults to each source's configured default_limit.",
    ),
    source: str | None = typer.Option(None, help="Only fetch one source id."),
    group: str | None = typer.Option(None, help="Only fetch one source group."),
) -> None:
    configure_logging()
    result = run_fetch_from_registry(
        settings=Settings.from_env(),
        limit_per_source=limit_per_source,
        source_filter=source,
        source_group_filter=group,
    )
    for source_id, stats in result.stats.items():
        typer.echo(
            f"{source_id}: fetched={stats.fetched} inserted={stats.inserted} "
            f"skipped={stats.skipped} failed={stats.failed}"
            + (f" error={stats.error}" if stats.error else "")
        )


@app.command("normalize")
def normalize(
    limit: int = typer.Option(100, min=1, help="Maximum raw_items to normalize in this run."),
) -> None:
    configure_logging()
    result = run_normalize_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


@app.command("prefilter")
def prefilter(
    limit: int = typer.Option(100, min=1, help="Maximum normalized_items to prefilter in this run."),
) -> None:
    configure_logging()
    result = run_prefilter_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed={result.processed} kept={result.kept} "
        f"dropped={result.dropped} failed={result.failed}"
    )


if __name__ == "__main__":
    app()
