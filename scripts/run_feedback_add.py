from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.feedback_job import add_feedback_from_settings
from app.logging_config import configure_logging


def main(
    action: str = typer.Argument(..., help="Feedback action: like/dislike/save/hide/click/report."),
    entity_id: int | None = typer.Option(None, help="Canonical entity id."),
    candidate_item_id: int | None = typer.Option(None, help="Candidate item id."),
    reason: str | None = typer.Option(None, help="Optional feedback reason."),
) -> None:
    configure_logging()
    result = add_feedback_from_settings(
        settings=Settings.from_env(),
        entity_id=entity_id,
        candidate_item_id=candidate_item_id,
        action=action,
        reason=reason,
    )
    typer.echo(f"inserted={result.inserted} feedback_id={result.feedback_id}")


if __name__ == "__main__":
    typer.run(main)
