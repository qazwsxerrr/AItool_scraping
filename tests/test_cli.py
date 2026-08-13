from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from app import main


runner = CliRunner()


def test_cli_registers_ai_only_commands():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in ("fetch", "fetch-only", "ai-review", "export", "run-once", "source-health"):
        assert command in result.stdout
