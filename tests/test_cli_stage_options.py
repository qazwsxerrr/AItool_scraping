from __future__ import annotations

from typer.testing import CliRunner

from app import main


runner = CliRunner()


def test_cli_exposes_ai_only_commands():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in ("fetch", "fetch-only", "ai-review", "export", "run-once", "source-health"):
        assert command in result.stdout
    for legacy in ("process", "run-daily", "enrich", "triage", "cluster", "compose", "daily-export", "claim-extract", "evidence-search", "ai-verify", "recommendation-write"):
        assert legacy not in result.stdout


def test_cli_rejects_unknown_legacy_command():
    result = runner.invoke(main.app, ["evidence-search"])
    assert result.exit_code != 0


def test_cli_accepts_content_class_and_force(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(main.Settings, "from_env", lambda: object())

    def fake_run(**kwargs):
        captured.update(kwargs)
        class Result:
            not_due_sources = []
            skipped_sources = []
            stats = {}
            total_fetched = total_inserted = total_skipped = total_failed = 0
        return Result()

    monkeypatch.setattr(main, "run_intel_fetch_from_settings", fake_run)
    result = runner.invoke(main.app, ["fetch", "--class", "project_tool", "--force", "--dry-run"])
    assert result.exit_code == 0
    assert captured["content_class"] == "project_tool"
    assert captured["force"] is True
    assert captured["dry_run"] is True
