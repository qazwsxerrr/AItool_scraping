from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.web.app import create_app


def test_web_home_and_all_pages_render_empty_state(tmp_path):
    db_path = tmp_path / "web.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    app = create_app(
        session_factory=session_factory,
        init_database=False,
        intel_output_root=tmp_path / "missing-output",
    )
    client = TestClient(app)

    home_response = client.get("/")
    all_response = client.get("/all")
    search_response = client.get("/search")
    github_response = client.get("/github")

    assert home_response.status_code == 200
    assert "暂无推荐卡片" in home_response.text
    assert all_response.status_code == 200
    assert "暂无动态" in all_response.text
    assert search_response.status_code == 200
    assert "输入关键词开始搜索" in search_response.text
    assert github_response.status_code == 200
    assert "暂无 GitHub 项目" in github_response.text


def test_web_github_metadata_fixture_renders_home_github_and_search(tmp_path):
    db_path = tmp_path / "web-github.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    intel_dir = tmp_path / "intel"
    intel_dir.mkdir()
    _write_github_metadata(intel_dir / "intel_items.jsonl")

    app = create_app(
        session_factory=session_factory,
        init_database=False,
        github_data_path=intel_dir,
    )
    client = TestClient(app)

    home_response = client.get("/")
    github_response = client.get("/github?language=Python&q=OmniRoute&min_stars=800")
    search_response = client.get("/search?q=OmniRoute")

    assert home_response.status_code == 200
    assert "本周 GitHub AI 项目热点" in home_response.text
    assert "example/OmniRoute" in home_response.text

    assert github_response.status_code == 200
    assert "GitHub 项目热点" in github_response.text
    assert "example/OmniRoute" in github_response.text
    assert "github-card" in github_response.text
    assert "8989" in github_response.text

    assert search_response.status_code == 200
    assert "GitHub Projects" in search_response.text
    assert "example/OmniRoute" in search_response.text


def _write_github_metadata(path) -> None:
    payload = {
        "id": 100,
        "source_id": "github_weekly_active_rag",
        "source_type": "github_api",
        "external_id": "github_repo:example/omniroute",
        "content_class": "project_tool",
        "status": "hotspot",
        "title": "GitHub repo: example/OmniRoute",
        "url": "https://github.com/example/OmniRoute",
        "summary": "OmniRoute 是一个 AI 网关项目，适合统一管理 LLM provider。",
        "published_at": "2026-07-01T00:00:00+00:00",
        "metrics": {
            "stars": 8989,
            "forks": 1423,
            "watchers": 8989,
            "open_issues": 138,
            "language": "Python",
            "license": "MIT",
            "pushed_at": "2026-07-01T09:27:17+00:00",
            "latest_release": "v1.0.0",
            "topics": ["OmniRoute", "MCP", "LLM", "gateway"],
        },
        "raw_payload": {"github_item_type": "repository", "full_name": "example/OmniRoute"},
        "ai": None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
