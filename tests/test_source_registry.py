from pathlib import Path

import pytest

from app.config.source_registry import load_source_registry
from app.domain.models import SourceSpec


REGISTRY_YAML = """
sources:
  - id: native_blog
    name: Native Blog
    transport: feed
    url: https://example.com/feed.xml
    enabled: true
    priority: 10
    fetch_interval: 3600
    feed:
      format: rss
      adapter: generic
  - id: rsshub_route
    name: RSSHub Route
    transport: rsshub
    url: ${RSSHUB_BASE_URL}/twitter/user/OpenAI
    enabled: true
    priority: 20
    fetch_interval: 3600
    feed:
      format: rss
      adapter: generic
  - id: disabled_future
    name: Disabled Future
    transport: rsshub
    url: ${RSSHUB_BASE_URL}/future/route
    enabled: false
    priority: 30
    fetch_interval: 3600
    feed:
      format: rss
      adapter: generic
"""


def write_registry(tmp_path: Path) -> Path:
    path = tmp_path / "source_registry.yaml"
    path.write_text(REGISTRY_YAML, encoding="utf-8")
    return path


def test_load_registry_skips_enabled_rsshub_source_when_base_url_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("RSSHUB_BASE_URL", raising=False)
    result = load_source_registry(write_registry(tmp_path), env={})

    assert [source.id for source in result.sources] == ["native_blog"]
    assert isinstance(result.sources[0], SourceSpec)
    assert result.skipped[0].source_id == "rsshub_route"
    assert "RSSHUB_BASE_URL" in result.skipped[0].reason


def test_load_registry_interpolates_rsshub_base_url_when_present(tmp_path):
    result = load_source_registry(
        write_registry(tmp_path), env={"RSSHUB_BASE_URL": "https://rsshub.example.com"}
    )

    assert [source.id for source in result.sources] == ["native_blog", "rsshub_route"]
    rsshub_source = result.sources[1]
    assert rsshub_source.transport == "rsshub"
    assert rsshub_source.feed.format == "rss"
    assert rsshub_source.url == "https://rsshub.example.com/twitter/user/OpenAI"


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
        assert linux_source.transport == "feed"
        assert linux_source.feed.format == "rss"
        assert linux_source.feed.adapter == "generic"
        assert linux_source.url == url


def test_default_registry_uses_canonical_github_modes_and_producthunt_atom():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    producthunt = source_by_id["producthunt_feed"]
    assert producthunt.transport == "feed"
    assert producthunt.feed.format == "atom"
    assert producthunt.feed.adapter == "producthunt"

    daily = source_by_id["github_trending_daily_native"]
    assert daily.transport == "github"
    assert daily.github.mode == "trending"
    assert daily.github.period == "daily"
    assert daily.url == "https://github.com/trending?since=daily"
    assert daily.selection_policy.mode == "github_trending"

    weekly = source_by_id["github_trending_weekly_native"]
    assert weekly.github.mode == "trending"
    assert weekly.github.period == "weekly"
    assert weekly.source_subtype == "trending_weekly"

    topic_ids = {
        "github_search_topic_llm",
        "github_search_topic_ai_agent",
        "github_search_topic_rag",
        "github_search_topic_vector_database",
        "github_search_topic_large_language_model",
        "github_search_topic_machine_learning",
    }
    assert topic_ids <= source_by_id.keys()
    for topic_id in topic_ids:
        source = source_by_id[topic_id]
        assert source.transport == "github"
        assert source.github.mode == "search"
        assert source.github.sort == "stars"
        assert source.github.order == "desc"
        assert source.github.pushed_days == 7
        assert source.selection_policy.min_stars == 100
        assert source.github.query.startswith("topic:")

    assert "github_trending_python_daily" not in source_by_id
    assert "github_trending_typescript_daily" not in source_by_id

    release_source = source_by_id["github_releases_ollama"]
    assert release_source.transport == "github"
    assert release_source.github.mode == "releases"
    assert release_source.url == "https://api.github.com/repos/ollama/ollama/releases"
    assert release_source.source_group == "github"
    assert release_source.source_subtype == "repo_releases"
    assert release_source.quality_weight == 0.80
    assert release_source.spam_risk == "low"


def test_default_registry_includes_verified_official_feed_expansion():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    expected = {
        "openrouter_blog": ("https://openrouter.ai/blog/feed.xml", "official_blog"),
        "google_research_blog": ("https://research.google/blog/rss/", "official_research"),
        "nvidia_ai_blog": ("https://blogs.nvidia.com/feed/", "official_blog"),
        "aws_machine_learning_blog": (
            "https://aws.amazon.com/blogs/machine-learning/feed/",
            "official_blog",
        ),
    }
    assert expected.keys() <= source_by_id.keys()
    for source_id, (url, group) in expected.items():
        source = source_by_id[source_id]
        assert source.transport == "feed"
        assert source.feed.format == "rss"
        assert source.feed.adapter == "generic"
        assert source.url == url
        assert source.source_group == group
        assert source.content_class == "official_model_company"
        assert source.verification_policy.mode == "official_direct_link"
        assert source.verification_policy.required is True
        assert source.selection_policy.keywords


def test_legacy_routing_fields_are_rejected(tmp_path):
    path = tmp_path / "source_registry.yaml"
    path.write_text(
        """
sources:
  - id: legacy
    name: Legacy
    type: rss
    url: https://example.test/feed.xml
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="type"):
        load_source_registry(path, env={})


def test_github_search_pushed_days_must_be_positive(tmp_path):
    path = tmp_path / "source_registry.yaml"
    path.write_text(
        """
sources:
  - id: invalid_github_search
    name: Invalid GitHub Search
    transport: github
    url: https://api.github.com/search/repositories
    github:
      mode: search
      query: agent
      pushed_days: 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="pushed_days"):
        load_source_registry(path, env={})


def test_producthunt_adapter_must_be_atom(tmp_path):
    path = tmp_path / "source_registry.yaml"
    path.write_text(
        """
sources:
  - id: invalid_producthunt
    name: Invalid Product Hunt
    transport: feed
    url: https://example.test/producthunt.rss
    feed:
      format: rss
      adapter: producthunt
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="producthunt.*atom"):
        load_source_registry(path, env={})
