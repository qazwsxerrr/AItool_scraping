from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, Source
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
    assert "暂无精选内容" in home_response.text
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
    assert "持久化项目介绍：统一管理 LLM provider。" in home_response.text

    assert github_response.status_code == 200
    assert "GitHub 项目热点" in github_response.text
    assert "example/OmniRoute" in github_response.text
    assert "github-card" in github_response.text
    assert "8989" in github_response.text
    assert "持久化项目介绍：统一管理 LLM provider。" in github_response.text
    assert "本周新增 180" in github_response.text
    assert "今日新增 42" in github_response.text

    assert search_response.status_code == 200
    assert "GitHub Projects" in search_response.text
    assert "example/OmniRoute" in search_response.text
    assert "持久化项目介绍：统一管理 LLM provider。" in search_response.text


def test_web_v2_filters_escape_content_and_drop_removed_stage_copy(tmp_path):
    db_path = tmp_path / "web-v2-contract.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        source = Source(
            id="official_contract",
            name="Official Contract",
            transport="feed",
            url="https://example.com/feed.xml",
            feed_format="rss",
            feed_adapter="generic",
            source_group="official_blog",
            source_subtype="official",
            source_role="official",
            content_class="official_model_company",
        )
        item = IntelItem(
            source_id=source.id,
            external_id="contract-item",
            title="<script>alert(1)</script> model update",
            canonical_url="https://example.com/item?a=1&b=2",
            summary="A persisted v2 item",
            content_class="official_model_company",
            content_hash="web-contract-item",
            published_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
            status="selected",
            selection_score=91,
        )
        session.add_all(
            [
                source,
                item,
                AIItemReview(
                    item=item,
                    keep=True,
                    content_class="official_model_company",
                    confidence=94,
                    summary_cn="AI 生成的更新摘要",
                    risk_flags_json=json.dumps(["风险一", "风险二", "风险三"], ensure_ascii=False),
                    raw_response_json="{}",
                ),
            ]
        )
        session.commit()

    app = create_app(session_factory=session_factory, init_database=False)
    client = TestClient(app)

    home_response = client.get("/")
    all_response = client.get("/all?status=selected&content_class=official_model_company")
    search_response = client.get("/search?q=script")

    assert home_response.status_code == 200
    assert "风险提示 3" in home_response.text
    assert 'class="badge danger"' not in home_response.text
    assert all_response.status_code == 200
    assert "selected" in all_response.text
    assert "官方发布" in all_response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in all_response.text
    assert "<script>alert(1)</script>" not in all_response.text
    assert "recommendation-write" not in all_response.text
    assert "Claim" not in all_response.text
    assert "Evidence" not in all_response.text
    assert search_response.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in search_response.text


def test_web_hotspot_zero_score_and_mobile_nav_contract(tmp_path):
    db_path = tmp_path / "web-hotspot-contract.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    long_summary = "README 完整说明。" * 100
    with session_factory() as session:
        source = Source(
            id="github_hotspot_contract",
            name="GitHub Hotspot",
            transport="github",
            url="https://github.com/trending",
            source_group="github",
            source_subtype="trending",
            content_class="project_tool",
        )
        session.add_all(
            [
                source,
                IntelItem(
                    source_id=source.id,
                    external_id="github_repo:contract/hotspot",
                    title="Contract hotspot",
                    canonical_url="https://github.com/contract/hotspot",
                    summary=long_summary,
                    content_class="project_tool",
                    content_hash="hotspot-zero-score",
                    status="hotspot",
                    selection_score=0,
                ),
            ]
        )
        session.commit()

    app = create_app(session_factory=session_factory, init_database=False)
    client = TestClient(app)
    home_response = client.get("/")
    search_response = client.get("/search?q=Contract+hotspot")
    css_response = client.get("/static/style.css")

    assert home_response.status_code == 200
    assert "精选时间线" in home_response.text
    assert "暂无精选内容" not in home_response.text
    assert "score-hot" in home_response.text
    assert "HOT" in home_response.text
    assert "score-low" not in home_response.text
    assert "summary-clamp" in home_response.text
    assert search_response.status_code == 200
    assert "score-hot" in search_response.text
    assert "HOT" in search_response.text
    assert "score-low" not in search_response.text
    assert css_response.status_code == 200
    assert ".sidebar .nav-item" in css_response.text
    assert "flex: 0 0 auto" in css_response.text
    assert "min-height: 44px" in css_response.text
    assert "white-space: nowrap" in css_response.text


def test_web_home_renders_ai_selected_without_verification_gate(tmp_path):
    db_path = tmp_path / "web-ai-only.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        source = Source(
            id="web_ai_only",
            name="AI-only feed",
            transport="feed",
            url="https://example.test/feed.xml",
            source_group="official_blog",
            source_subtype="fixed_news",
            source_role="official",
            content_class="official_model_company",
        )
        item = IntelItem(
            source_id=source.id,
            external_id="web-ai-only-1",
            title="AI-only homepage item",
            canonical_url="https://example.test/ai-only",
            summary="source summary",
            content_class="official_model_company",
            content_hash="web-ai-only-contract",
            selection_score=86,
            status="selected",
        )
        session.add_all(
            [
                source,
                item,
                AIItemReview(
                    item=item,
                    status="success",
                    keep=True,
                    content_class="official_model_company",
                    confidence=93,
                    summary_cn="首页 AI 摘要",
                    raw_response_json="{}",
                ),
            ]
        )
        session.commit()

    app = create_app(session_factory=session_factory, init_database=False)
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "AI-only homepage item" in response.text
    assert "首页 AI 摘要" in response.text
    assert "AI 已选" in response.text
    assert "核实" not in response.text


def _write_github_metadata(path) -> None:
    payload = {
        "id": 100,
        "source_id": "github_weekly_active_rag",
        "source_transport": "github",
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
            "trending": {
                "weekly": {"rank": 1, "stars_since": 180},
                "daily": {"rank": 2, "stars_since": 42},
            },
        },
        "raw_payload": {"github_item_type": "repository", "full_name": "example/OmniRoute"},
        "ai": {"status": "success", "summary_cn": "持久化项目介绍：统一管理 LLM provider。"},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
