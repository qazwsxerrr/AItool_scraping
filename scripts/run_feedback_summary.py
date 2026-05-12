from __future__ import annotations

import json
from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.feedback_job import feedback_summary_from_settings
from app.logging_config import configure_logging


def main(
    entity_id: int | None = typer.Option(None, help="Canonical entity id."),
    candidate_item_id: int | None = typer.Option(None, help="Candidate item id."),
) -> None:
    configure_logging()
    summary = feedback_summary_from_settings(
        settings=Settings.from_env(),
        entity_id=entity_id,
        candidate_item_id=candidate_item_id,
    )
    typer.echo(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    typer.run(main)
