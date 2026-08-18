from __future__ import annotations

from typer.testing import CliRunner

from app import main


runner = CliRunner()


def test_cli_exposes_ai_only_commands():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in ("fetch", "fetch-only", "ai-review", "stage-d", "export", "run-once", "source-health"):
        assert command in result.stdout
    for legacy in ("process", "run-daily", "enrich", "triage", "cluster", "compose", "rank", "editorial-rank", "daily-export", "claim-extract", "evidence-search", "ai-verify", "recommendation-write"):
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


def test_cli_accepts_news_media_content_class(monkeypatch):
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
    result = runner.invoke(main.app, ["fetch", "--class", "news_media", "--dry-run"])

    assert result.exit_code == 0
    assert captured["content_class"] == "news_media"


def test_pipeline_export_prints_the_public_edition_not_the_internal_run_id(monkeypatch, tmp_path):
    monkeypatch.setattr(main.Settings, "from_env", lambda: object())

    class Result:
        exported = 1
        partial = False
        markdown_path = str(tmp_path / "daily" / "2026-08-16" / "intel_digest.md")
        jsonl_path = str(tmp_path / "daily" / "2026-08-16" / "intel_items.jsonl")
        manifest_path = str(tmp_path / "daily" / "2026-08-16" / "manifest.json")

    monkeypatch.setattr(main, "run_pipeline_export_from_settings", lambda **kwargs: Result())

    result = runner.invoke(main.app, ["pipeline", "export", "--run-id", "77", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "edition_date=2026-08-16" in result.stdout
    assert "run_id=77" not in result.stdout
    assert "snapshot=" not in result.stdout
