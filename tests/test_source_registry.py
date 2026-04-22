from pathlib import Path

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
