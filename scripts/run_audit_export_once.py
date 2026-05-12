from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import Settings
from app.jobs.recommendation_export_job import run_audit_export_from_settings
from app.logging_config import configure_logging


def main(
    limit: int = typer.Option(100, min=1, help="Maximum verification_items to export for audit."),
    output_dir: str = typer.Option("output", help="Directory for Markdown and JSONL audit files."),
) -> None:
    configure_logging()
    result = run_audit_export_from_settings(
        settings=Settings.from_env(),
        output_dir=output_dir,
        limit=limit,
    )
    typer.echo(f"exported={result.exported}")
    typer.echo(f"markdown={result.markdown_path}")
    typer.echo(f"jsonl={result.jsonl_path}")


if __name__ == "__main__":
    typer.run(main)
