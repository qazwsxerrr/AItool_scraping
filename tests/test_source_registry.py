from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config.source_registry import load_source_registry
from app.domain import FetchItem, selection_decision
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
    assert release_source.source_group == "github_release"
    assert release_source.source_subtype == "repo_releases"
    assert release_source.quality_weight == 0.80
    assert release_source.spam_risk == "low"


def test_default_registry_includes_official_feed_expansion():
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
        assert source.selection_policy.keywords


def test_default_registry_includes_gap_p0_p1_sources():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    source_by_id = {source.id: source for source in result.sources}

    official_sources = {
        "langchain_blog": (
            "feed",
            "https://www.langchain.com/blog/rss.xml",
            "developer_blog",
        ),
        "google_developers_blog": (
            "feed",
            "https://developers.googleblog.com/feeds/posts/default/?alt=rss",
            "developer_blog",
        ),
        "cursor_changelog": (
            "feed",
            "https://cursor.com/changelog/rss.xml",
            "product_changelog",
        ),
        "deepseek_api_news_rsshub": (
            "rsshub",
            "https://rsshub.example/deepseek/news",
            "api_news",
        ),
    }
    for source_id, (transport, url, subtype) in official_sources.items():
        source = source_by_id[source_id]
        assert source.transport == transport
        assert source.url == url
        assert source.source_group == "official_blog"
        assert source.source_subtype == subtype
        assert source.source_role == "official"
        assert source.content_class == "official_model_company"
        assert source.tier == "p1"
        assert source.primary_eligible is True
        assert source.selection_policy.mode == "first_party_recent"

    gary_marcus = source_by_id["gary_marcus_blog"]
    assert gary_marcus.url == "https://garymarcus.substack.com/feed"
    assert gary_marcus.source_group == "tech_media"
    assert gary_marcus.source_role == "analysis"
    assert gary_marcus.content_class == "news_media"
    assert gary_marcus.selection_policy.mode == "media_recent"
    assert gary_marcus.selection_policy.keywords == ()

    tomer_tunguz = source_by_id["tomer_tunguz_blog"]
    assert tomer_tunguz.url == "https://www.tomtunguz.com/index.xml"
    assert tomer_tunguz.source_group == "tech_media"
    assert tomer_tunguz.source_role == "analysis"
    assert tomer_tunguz.content_class == "news_media"
    assert tomer_tunguz.selection_policy.mode == "media_recent"
    assert tomer_tunguz.selection_policy.keywords == ()


def test_default_registry_includes_verified_aihot_feed_expansion():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    expected = {
        "google_blog_ai": ("https://blog.google/rss/", "official_blog", "official_model_company"),
        "ithome_ai_news": ("https://www.ithome.com/rss/", "tech_media", "news_media"),
        "the_decoder_ai_news": ("https://the-decoder.com/feed/", "tech_media", "news_media"),
        "techcrunch_ai": ("https://techcrunch.com/category/artificial-intelligence/feed/", "tech_media", "news_media"),
        "the_verge_ai": ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "tech_media", "news_media"),
        "hacker_news_ai": ("https://hnrss.org/newest?q=AI", "hacker_news", "community_social"),
    }
    for source_id, (url, group, content_class) in expected.items():
        source = source_by_id[source_id]
        assert source.url == url
        assert source.source_group == group
        assert source.content_class == content_class
        assert source.feed is not None

    assert source_by_id["the_verge_ai"].feed.format == "atom"
    assert source_by_id["ithome_ai_news"].selection_policy.max_age_days == 3
    assert source_by_id["ithome_ai_news"].bypass_proxy is True
    assert source_by_id["hacker_news_ai"].bypass_proxy is True
    assert source_by_id["the_decoder_ai_news"].selection_policy.keywords == ()


def test_default_registry_includes_aihot_official_x_accounts():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    source_by_id = {source.id: source for source in result.sources}

    expected_handles = {
        "x_account_baidu": "Baidu_Inc",
        "x_account_alibaba_cloud": "alibaba_cloud",
        "x_account_siliconflow": "SiliconFlowAI",
        "x_account_openrouter": "OpenRouter",
        "x_account_runway": "runwayml",
        "x_account_replit": "Replit",
        "x_account_openbmb": "OpenBMB",
        "x_account_sensetime": "SenseTime_AI",
        "x_account_antling": "AntLingAGI",
    }
    for source_id, handle in expected_handles.items():
        source = source_by_id[source_id]
        assert source.source_group == "x_official"
        assert source.content_class == "official_model_company"
        assert source.tier == "p1"
        assert source.primary_eligible is True
        assert source.selection_policy.mode == "first_party_recent"
        assert source.selection_policy.max_age_days == 7
        assert source.url.endswith(f"/twitter/user/{handle}")
        assert source.account_url == f"https://x.com/{handle}"


def test_default_registry_treats_gap_x_accounts_as_first_party_sources():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    source_by_id = {source.id: source for source in result.sources}
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    expected_handles = {
        "x_account_claude_devs": "ClaudeDevs",
        "x_account_pvncher": "pvncher",
        "x_account_thdxr": "thdxr",
        "x_account_xhyctf": "xhyctf",
        "x_account_devin_desktop": "devindesktop",
        "x_account_cursor_ai": "cursor_ai",
    }
    for source_id, handle in expected_handles.items():
        source = source_by_id[source_id]
        assert source.transport == "rsshub"
        assert source.source_group == "x_official"
        assert source.source_subtype == "account"
        assert source.source_role == "official"
        assert source.content_class == "official_model_company"
        assert source.tier == "p1"
        assert source.primary_eligible is True
        assert source.selection_policy.mode == "first_party_recent"
        assert source.selection_policy.max_age_days == 7
        assert source.url == f"https://rsshub.example/twitter/user/{handle}"
        assert source.account_url == f"https://x.com/{handle}"
        decision = selection_decision(
            FetchItem(
                source_id=source_id,
                title="A first-party product update without generic AI keywords",
                url=f"https://x.com/{handle}/status/1",
                published_at=now,
            ),
            source,
            now=now,
        )
        assert decision.selected is True
        assert decision.reason == "selected:first_party_recent"


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


def test_registry_rejects_duplicate_source_ids(tmp_path):
    path = tmp_path / "source_registry.yaml"
    path.write_text(
        """
sources:
  - id: duplicate
    transport: feed
    url: https://example.test/one.xml
  - id: duplicate
    transport: feed
    url: https://example.test/two.xml
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate source id: duplicate"):
        load_source_registry(path, env={})


def test_enabled_sources_use_canonical_groups_and_governance_defaults():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    canonical = {
        "official_blog",
        "official_research",
        "github_trending",
        "github_release",
        "github_search",
        "producthunt",
        "hacker_news",
        "reddit_fixed",
        "reddit_search",
        "linux_do",
        "x_official",
        "x_social",
        "x_search",
        "tech_media",
    }
    assert result.sources
    assert all(source.source_group in canonical for source in result.sources)
    assert all(source.tier in {"p1", "p2", "p3", "p4"} for source in result.sources)
    assert all(source.topic_scopes for source in result.sources)
    first_party_x_sources = [
        source
        for source in result.sources
        if source.source_group == "x_official"
        and source.source_role == "official"
        and source.source_subtype == "account"
    ]
    assert first_party_x_sources
    assert all(source.content_class == "official_model_company" for source in first_party_x_sources)
    assert all(source.tier == "p1" for source in first_party_x_sources)
    assert all(source.primary_eligible for source in first_party_x_sources)
    assert all(source.selection_policy.mode == "first_party_recent" for source in first_party_x_sources)
    assert all(source.selection_policy.max_age_days == 7 for source in first_party_x_sources)
    assert all(
        not source.primary_eligible
        for source in result.sources
        if source.source_group in {"x_social", "x_search"}
    )


def test_default_registry_contains_v3_official_feeds_and_x_handles():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    source_by_id = {source.id: source for source in result.sources}
    assert source_by_id["anthropic_news_rsshub"].source_group == "official_blog"
    assert source_by_id["anthropic_research_rsshub"].source_group == "official_research"
    assert source_by_id["anthropic_research_rsshub"].url == "https://rsshub.example/anthropic/research?limit=6"
    assert source_by_id["anthropic_engineering_rsshub"].source_group == "official_research"
    assert source_by_id["x_account_chatgpt"].url.endswith("/twitter/user/ChatGPT")
    assert source_by_id["x_account_xai"].url.endswith("/twitter/user/spacexai")
    for source_id, handle in {
        "x_account_zai_org": "Zai_org",
        "x_account_kimi_moonshot": "Kimi_Moonshot",
        "x_account_minimax_ai": "MiniMax_AI",
    }.items():
        source = source_by_id[source_id]
        assert source.source_group == "x_official"
        assert source.content_class == "official_model_company"
        assert source.tier == "p1"
        assert source.primary_eligible is True
        assert source.selection_policy.mode == "first_party_recent"
        assert handle in source.url
        assert source.account_url == f"https://x.com/{handle}"
