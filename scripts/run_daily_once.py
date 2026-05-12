from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.pipeline_run_job import run_daily_from_settings
from app.logging_config import configure_logging


def main() -> None:
    configure_logging()
    result = run_daily_from_settings(settings=Settings.from_env())
    typer.echo(f"run_id={result.run_id} status={result.status}")
    if result.error:
        typer.echo(f"error={result.error}")


if __name__ == "__main__":
    typer.run(main)
