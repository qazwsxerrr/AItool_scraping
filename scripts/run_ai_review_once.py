from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.ai_review_job import run_ai_review_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(5, min=1, help="Maximum kept candidate_items to review with AI."),
) -> None:
    configure_logging()
    result = run_ai_review_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"AI review stats: processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    if result.errors:
        typer.echo("Errors:")
        for error in result.errors:
            typer.echo(f"  - {error}")


if __name__ == "__main__":
    typer.run(main)
