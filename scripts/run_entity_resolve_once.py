from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.entity_resolve_job import run_entity_resolve_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(100, min=1, help="Maximum verification_items to resolve into canonical entities."),
) -> None:
    configure_logging()
    result = run_entity_resolve_from_settings(settings=Settings.from_env(), limit=limit)
    typer.echo(
        f"processed={result.processed} entities_created={result.entities_created} "
        f"mentions_created={result.mentions_created} failed={result.failed}"
    )


if __name__ == "__main__":
    typer.run(main)
