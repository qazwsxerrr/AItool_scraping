from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.claim_verify_job import run_claim_verify_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(100, min=1, help="Maximum extracted_claims to verify at claim level."),
) -> None:
    configure_logging()
    result = run_claim_verify_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed_claims={result.processed_claims} inserted={result.inserted} "
        f"skipped={result.skipped} failed={result.failed}"
    )


if __name__ == "__main__":
    typer.run(main)
