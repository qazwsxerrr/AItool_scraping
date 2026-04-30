from __future__ import annotations

import typer

from app.config.settings import Settings
from app.jobs.ai_review_job import run_ai_review_from_settings
from app.jobs.fetch_job import run_fetch_from_registry
from app.jobs.normalize_job import run_normalize_from_settings
from app.jobs.prefilter_job import run_prefilter_from_settings
from app.jobs.review_export_job import run_review_export_from_settings
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


@app.command("review-export")
def review_export(
    limit: int = typer.Option(50, min=1, help="Maximum candidate_items to export."),
    output_dir: str = typer.Option("output", help="Directory for Markdown and JSONL review files."),
    status: str = typer.Option("kept", help="Candidate status to export, usually kept or dropped."),
) -> None:
    configure_logging()
    result = run_review_export_from_settings(
        settings=Settings.from_env(),
        output_dir=output_dir,
        limit=limit,
        status=status,
    )
    typer.echo(f"exported={result.exported}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"jsonl={result.jsonl_path}")


@app.command("ai-review")
def ai_review(
    limit: int = typer.Option(5, min=1, help="Maximum high-score candidate_items to review with AI."),
    min_score: int | None = typer.Option(
        None,
        min=0,
        max=100,
        help="Minimum candidate_score to send to AI. Defaults to AI_REVIEW_MIN_CANDIDATE_SCORE.",
    ),
) -> None:
    configure_logging()
    result = run_ai_review_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        min_candidate_score=min_score,
    )
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


if __name__ == "__main__":
    app()
