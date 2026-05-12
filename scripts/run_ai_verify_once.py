from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.ai_verify_job import run_ai_verify_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(30, min=1, help="Maximum evidence-backed candidates to verify with AI."),
) -> None:
    configure_logging()
    result = run_ai_verify_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed={result.processed} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


if __name__ == "__main__":
    typer.run(main)
