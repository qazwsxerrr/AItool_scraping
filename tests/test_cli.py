from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from app import main


runner = CliRunner()


def test_cli_registers_v3_daily_commands():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "source-health",
        "enrich",
        "triage",
        "cluster",
        "compose",
        "daily-export",
        "run-daily",
    ):
        assert command in result.stdout


def test_cli_daily_export_command_accepts_date_and_output(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    monkeypatch.setattr(main.Settings, "from_env", lambda: object())

    def fake_export(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            edition_date="2026-08-12",
            status="blocked",
            selected=0,
            published=False,
            markdown_path=str(tmp_path / "daily.md"),
            draft_path=str(tmp_path / "daily.draft.md"),
        )

    monkeypatch.setattr(main, "run_daily_export_from_settings", fake_export)
    result = runner.invoke(
        main.app,
        ["daily-export", "--date", "2026-08-12", "--output-dir", str(tmp_path), "--force"],
    )
    assert result.exit_code == 0
    assert captured["edition_date"] == "2026-08-12"
    assert captured["output_dir"] == str(tmp_path)
    assert captured["force"] is True
    assert "status=blocked" in result.stdout
