from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.evidence_classify_job import run_evidence_classify_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(100, min=1, help="Maximum fetched evidence_items to classify."),
) -> None:
    configure_logging()
    result = run_evidence_classify_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(f"processed={result.processed} updated={result.updated} failed={result.failed}")


if __name__ == "__main__":
    typer.run(main)
