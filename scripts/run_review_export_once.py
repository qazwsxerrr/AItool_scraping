from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.review_export_job import run_review_export_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(50, min=1, help="Maximum candidate_items to export."),
    output_dir: Path = typer.Option(Path("output"), help="Directory for Markdown and JSONL review files."),
    status: str = typer.Option("kept", help="Candidate status to export, usually kept or dropped."),
) -> None:
    configure_logging()
    result = run_review_export_from_settings(
        settings=Settings.from_env(),
        output_dir=output_dir,
        limit=limit,
        status=status,
    )
    typer.echo(f"Review export stats: exported={result.exported}")
    typer.echo(f"Markdown: {result.markdown_path}")
    typer.echo(f"JSONL: {result.jsonl_path}")


if __name__ == "__main__":
    typer.run(main)
