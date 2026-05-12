from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.claim_extract_job import run_claim_extract_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(50, min=1, help="Maximum AI-reviewed candidate_items to extract claims from."),
    min_ai_score: int | None = typer.Option(None, min=0, max=100, help="Minimum ai_score to process."),
) -> None:
    configure_logging()
    result = run_claim_extract_from_settings(
        settings=Settings.from_env(),
        limit=limit,
        min_ai_score=min_ai_score,
    )
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


if __name__ == "__main__":
    typer.run(main)
