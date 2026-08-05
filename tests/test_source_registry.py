from pathlib import Path

import pytest

from app.config.source_registry import load_source_registry


REGISTRY_YAML = """
sources:
  - id: native_blog
    name: Native Blog
    type: rss
    url: https://example.com/feed.xml
    enabled: true
    priority: 10
    fetch_interval: 3600
    parser_type: feedparser
  - id: rsshub_route
    name: RSSHub Route
    type: rsshub
    url: ${RSSHUB_BASE_URL}/github/trending/daily/python
    enabled: true
    priority: 20
    fetch_interval: 3600
    parser_type: feedparser
  - id: disabled_future
    name: Disabled Future
    type: rsshub
    url: ${RSSHUB_BASE_URL}/future/route
    enabled: false
    priority: 30
    fetch_interval: 3600
    parser_type: feedparser
"""


def write_registry(tmp_path: Path) -> Path:
    path = tmp_path / "source_registry.yaml"
    path.write_text(REGISTRY_YAML, encoding="utf-8")
    return path


def test_load_registry_skips_enabled_rsshub_source_when_base_url_missing(tmp_path):
    result = load_source_registry(write_registry(tmp_path), env={})

    assert [source.id for source in result.sources] == ["native_blog"]
    assert result.skipped[0].source_id == "rsshub_route"
    assert "RSSHUB_BASE_URL" in result.skipped[0].reason


def test_load_registry_interpolates_rsshub_base_url_when_present(tmp_path):
    result = load_source_registry(
        write_registry(tmp_path), env={"RSSHUB_BASE_URL": "https://rsshub.example.com"}
    )

    assert [source.id for source in result.sources] == ["native_blog", "rsshub_route"]
    rsshub_source = result.sources[1]
    assert rsshub_source.url == "https://rsshub.example.com/github/trending/daily/python"


def test_default_registry_includes_linux_do_sources():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    expected_sources = {
        "linux_do_top": ("LINUX DO Top Topics", "https://linux.do/top.rss"),
        "linux_do_hot": ("LINUX DO Hot Topics", "https://linux.do/hot.rss"),
    }
    assert "linux_do_latest" not in source_by_id

    for source_id, (name, url) in expected_sources.items():
        linux_source = source_by_id[source_id]
        assert linux_source.name == name
        assert linux_source.type == "rss"
        assert linux_source.url == url
        assert linux_source.parser_type == "feedparser"


def test_default_registry_includes_github_api_sources():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    search_source = source_by_id["github_search_ai_tools"]
    assert search_source.type == "github_api"
    assert search_source.url == "https://api.github.com/search/repositories"
    assert search_source.source_group == "github"
    assert search_source.source_subtype == "search_repositories"
    assert search_source.source_role == "code_hosting"
    assert search_source.spam_risk == "medium"
    assert search_source.requires_verification is False
    assert search_source.content_class == "project_tool"
    assert search_source.collector_type == "github"
    assert search_source.verification_policy["mode"] == "metadata_only"
    assert search_source.search_query is not None
    assert "mcp" in search_source.search_query
    assert "stars:>100" in search_source.search_query
    assert search_source.search_pushed_days == 30

    active_source = source_by_id["github_search_ai_active_high_star"]
    assert active_source.search_sort == "stars"
    assert active_source.search_order == "desc"
    assert active_source.search_pushed_days == 30
    assert "stars:>100" in (active_source.search_query or "")
    assert "LLM OR MCP OR RAG" in (active_source.search_query or "")
    assert "in:name,description" in (active_source.search_query or "")
    assert "in:readme" not in (active_source.search_query or "")

    release_source = source_by_id["github_releases_ollama"]
    assert release_source.type == "github_api"
    assert release_source.url == "https://api.github.com/repos/ollama/ollama/releases"
    assert release_source.source_group == "github"
    assert release_source.source_subtype == "repo_releases"
    assert release_source.quality_weight == 0.80
    assert release_source.spam_risk == "low"


def test_github_search_pushed_days_must_be_positive(tmp_path):
    path = tmp_path / "source_registry.yaml"
    path.write_text(
        """
sources:
  - id: invalid_github_search
    name: Invalid GitHub Search
    type: github_api
    url: https://api.github.com/search/repositories
    source_subtype: search_repositories
    search_query: agent
    search_pushed_days: 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="search_pushed_days"):
        load_source_registry(path, env={})
