from __future__ import annotations

from typer.testing import CliRunner

from app import main


runner = CliRunner()


def test_cli_exposes_current_daily_commands_only():
    result = runner.invoke(main.app, ["--help"])
    assert result.exit_code == 0
    for command in ("fetch", "fetch-only", "run-once", "source-health", "pipeline"):
        assert command in result.stdout
    for legacy in ("ai-review", "stage-d", "adopt-existing", "process", "run-daily", "enrich", "triage", "cluster", "compose", "rank", "editorial-rank", "daily-export", "claim-extract", "evidence-search", "ai-verify", "recommendation-write"):
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
    result = runner.invoke(main.app, ["fetch", "--class", "project_tool", "--force"])
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
    result = runner.invoke(main.app, ["fetch", "--class", "news_media"])

    assert result.exit_code == 0
    assert captured["content_class"] == "news_media"


def test_pipeline_export_uses_the_public_edition_not_the_internal_run_id(monkeypatch, tmp_path):
    monkeypatch.setattr(
        main.Settings,
        "from_env",
        lambda: type("Settings", (), {"database_url": f"sqlite:///{tmp_path / 'daily.db'}"})(),
    )
    captured: dict[str, object] = {}

    class Result:
        exported = 1
        partial = False
        markdown_path = str(tmp_path / "daily" / "2026-08-16" / "intel_digest.md")
        jsonl_path = str(tmp_path / "daily" / "2026-08-16" / "intel_items.jsonl")
        manifest_path = str(tmp_path / "daily" / "2026-08-16" / "manifest.json")

    def export_run(**kwargs):
        captured["edition_date"] = kwargs["edition_date"]
        return Result()

    monkeypatch.setattr(main, "publish_daily_draft_from_settings", export_run)

    result = runner.invoke(main.app, ["pipeline", "export", "--edition-date", "2026-08-16", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert captured == {"edition_date": "2026-08-16"}
    assert "edition_date=2026-08-16" in result.stdout
    assert "audit=" in result.stdout
    assert "run_id=" not in result.stdout
    assert "snapshot=" not in result.stdout


def test_pipeline_commands_no_longer_accept_run_id_as_a_public_option():
    result = runner.invoke(main.app, ["pipeline", "status", "--run-id", "77"])

    assert result.exit_code != 0


def test_formal_daily_commands_reject_source_and_class_overrides():
    for arguments in (
        ["pipeline", "run", "--source", "one"],
        ["pipeline", "start", "--class", "news_media"],
        ["pipeline", "stage-a", "--edition-date", "2026-08-19", "--source", "one"],
        ["pipeline", "export", "--edition-date", "2026-08-19", "--class", "news_media"],
        ["run-once", "--source", "one"],
    ):
        result = runner.invoke(main.app, arguments)
        assert result.exit_code != 0, arguments
