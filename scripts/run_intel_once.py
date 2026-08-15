"""Thin scheduled-task wrapper for the fetch -> AI review -> export flow."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.limits import DEFAULT_AI_REVIEW_LIMIT, DEFAULT_FETCH_LIMIT_PER_SOURCE
from app.config.settings import Settings
from app.jobs.run_job import run_intel_once_from_settings
from app.logging_config import configure_logging


def main(
    source: str | None = typer.Option(None),
    content_class: str | None = typer.Option(None, "--class"),
    limit: int = typer.Option(DEFAULT_FETCH_LIMIT_PER_SOURCE, "--limit", "--fetch-limit", min=1),
    ai_limit: int = typer.Option(DEFAULT_AI_REVIEW_LIMIT, "--ai-limit", min=1),
    force: bool = typer.Option(False, "--force"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    output_dir: str = typer.Option("output/intel"),
) -> None:
    configure_logging()
    result = run_intel_once_from_settings(
        settings=Settings.from_env(),
        source=source,
        content_class=content_class,
        limit=limit,
        ai_limit=ai_limit,
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
        f"analyzed={result.ai_review.analyzed} failed={result.ai_review.failed}"
    )
    typer.echo(f"export: exported={result.export.exported} pending={result.export.pending}")
    typer.echo(f"run_id={result.run_id} status={result.status}")
    if result.status == "failed":
        raise typer.Exit(code=1)


if __name__ == "__main__":
    typer.run(main)
