from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from app import main


runner = CliRunner()


def test_cli_registers_only_current_daily_commands():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in ("fetch", "fetch-only", "run-once", "source-health", "pipeline"):
        assert command in result.stdout
    for removed in ("ai-review", "stage-d", "adopt-existing"):
        assert removed not in result.stdout
