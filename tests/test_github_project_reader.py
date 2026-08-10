from __future__ import annotations

import json

from app.storage.github_reader import GitHubProjectFilters, GitHubProjectReader


def test_github_project_reader_filters_and_searches_metadata(tmp_path):
    output_dir = tmp_path / "intel"
    output_dir.mkdir()
    _write_jsonl(
        output_dir / "intel_items.jsonl",
        [
            _project_payload(
                repo="example/mcp-agent",
                summary="一个 MCP Agent 工具",
                stars=1200,
                forks=100,
                language="Python",
                topics=["MCP", "Agent"],
            ),
            _project_payload(
                repo="example/old-wrapper",
                summary="低活跃包装项目",
                stars=55,
                forks=8,
                language="TypeScript",
                archived=True,
                license_name=None,
            ),
        ],
    )

    reader = GitHubProjectReader(data_path=output_dir)
    all_rows = reader.list_projects()
    filtered = reader.list_projects(
        filters=GitHubProjectFilters(query="MCP", language="Python", min_stars=80)
    )
    search_rows = reader.search("agent")

    assert all_rows.stats.total == 2
    assert all_rows.stats.active_count == 1
    assert all_rows.stats.archived_count == 1
    assert all_rows.stats.risky_count == 1
    assert all_rows.stats.max_stars == 1200
    assert [row.repo_full_name for row in all_rows.rows] == [
        "example/mcp-agent",
        "example/old-wrapper",
    ]
    assert [row.repo_full_name for row in filtered.rows] == ["example/mcp-agent"]
    assert [row.repo_full_name for row in search_rows] == ["example/mcp-agent"]


def test_github_project_reader_limit_does_not_truncate_stats(tmp_path):
    output_dir = tmp_path / "intel"
    output_dir.mkdir()
    _write_jsonl(
        output_dir / "intel_items.jsonl",
        [
            _project_payload(repo="example/high", stars=950),
            _project_payload(repo="example/mid", stars=750),
        ],
    )

    result = GitHubProjectReader(output_root=tmp_path).list_projects(limit=1)

    assert len(result.rows) == 1
    assert result.rows[0].repo_full_name == "example/high"
    assert result.stats.total == 2


def test_github_project_reader_uses_persisted_ai_summary_and_period_stars(tmp_path):
    output_dir = tmp_path / "intel"
    output_dir.mkdir()
    payload = _project_payload(repo="example/ai-project", summary="collector description", stars=1200)
    payload["ai"] = {
        "summary_cn": "持久化项目介绍：用于构建 AI 工作流。",
        "risk_flags": ["README 信息可能过时"],
        "status": "success",
    }
    payload["metrics"]["trending"] = {
        "weekly": {"rank": 2, "stars_since": 180},
        "daily": {"rank": 1, "stars_since": 42},
    }
    _write_jsonl(output_dir / "intel_items.jsonl", [payload])

    row = GitHubProjectReader(data_path=output_dir).list_projects().rows[0]

    assert row.ai_summary == "持久化项目介绍：用于构建 AI 工作流。"
    assert row.summary == row.ai_summary
    assert row.weekly_stars_since == 180
    assert row.daily_stars_since == 42
    assert "README 信息可能过时" in row.risk_flags


def _write_jsonl(path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _project_payload(
    *,
    repo: str,
    summary: str = "AI 项目摘要",
    stars: int,
    forks: int = 100,
    language: str = "Python",
    topics: list[str] | None = None,
    archived: bool = False,
    license_name: str | None = "MIT",
) -> dict:
    return {
        "id": stars,
        "source_id": "github_active_high_star",
        "source_transport": "github",
        "external_id": f"github_repo:{repo}",
        "content_class": "project_tool",
        "status": "hotspot",
        "title": f"GitHub repo: {repo}",
        "url": f"https://github.com/{repo}",
        "summary": summary,
        "published_at": "2026-08-01T00:00:00+00:00",
        "metrics": {
            "stars": stars,
            "forks": forks,
            "watchers": stars,
            "open_issues": 12,
            "language": language,
            "license": license_name,
            "pushed_at": "2026-08-04T08:00:00+00:00",
            "topics": topics or ["AI"],
            "archived": archived,
        },
        "raw_payload": {
            "github_item_type": "repository",
            "full_name": repo,
            "license": {"spdx_id": license_name} if license_name else None,
        },
        "ai": None,
    }
