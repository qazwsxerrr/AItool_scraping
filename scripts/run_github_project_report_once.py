from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.github_project_report_job import run_github_project_report_from_settings
from app.logging_config import configure_logging


def main(
    database_url: str | None = typer.Option(
        None,
        help="Override DATABASE_URL. Example: sqlite:///./data/github_weekly_bucketed_20260629_123412.db",
    ),
    output_dir: str = typer.Option("output/github_project_report", help="Output directory for GitHub project reports."),
    limit: int | None = typer.Option(None, min=1, help="Maximum GitHub projects to process."),
    use_ai: bool = typer.Option(True, help="Use the configured AI model to generate Chinese project digests."),
    enrich: bool = typer.Option(True, help="Fetch repo detail, README, languages, and releases from GitHub API."),
    hot_min_score: int = typer.Option(60, min=0, max=100, help="Minimum GitHub final score for hotlist export."),
) -> None:
    configure_logging()
    settings = Settings.from_env()
    if database_url:
        settings = replace(settings, database_url=database_url)

    result = run_github_project_report_from_settings(
        settings=settings,
        output_dir=output_dir,
        limit=limit,
        use_ai=use_ai,
        enrich=enrich,
        hot_min_score=hot_min_score,
    )
    typer.echo(
        f"GitHub project report: processed={result.processed} ai_digested={result.ai_digested} "
        f"fallback_digested={result.fallback_digested} hotlist={result.hotlist_count} failed={result.failed}"
    )
    if result.output_paths:
        typer.echo(f"digest_markdown={result.output_paths.digest_markdown}")
        typer.echo(f"digest_jsonl={result.output_paths.digest_jsonl}")
        typer.echo(f"hotlist_markdown={result.output_paths.hotlist_markdown}")
        typer.echo(f"hotlist_jsonl={result.output_paths.hotlist_jsonl}")
        typer.echo(f"audit_markdown={result.output_paths.audit_markdown}")
    if result.errors:
        typer.echo("Errors:")
        for error in result.errors[:20]:
            typer.echo(f"  - {error}")


if __name__ == "__main__":
    typer.run(main)
