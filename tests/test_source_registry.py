from pathlib import Path

import pytest
import yaml

from app.config.source_registry import load_source_registry
from app.domain.models import SourceSpec


REGISTRY_YAML = """
sources:
  - id: native_blog
    name: Native Blog
    transport: feed
    url: https://example.com/feed.xml
    source_group: official_blog
  - id: rsshub_route
    name: RSSHub Route
    transport: rsshub
    url: ${RSSHUB_BASE_URL}/twitter/user/OpenAI
    source_group: x_official
  - id: disabled_future
    name: Disabled Future
    transport: rsshub
    url: ${RSSHUB_BASE_URL}/future/route
    enabled: false
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
    assert result.sources[0].content_class == "official_model_company"
    assert result.skipped[0].source_id == "rsshub_route"
    assert "RSSHUB_BASE_URL" in result.skipped[0].reason


def test_load_registry_interpolates_rsshub_base_url_and_derives_class(tmp_path):
    result = load_source_registry(
        write_registry(tmp_path), env={"RSSHUB_BASE_URL": "https://rsshub.example.com"}
    )

    assert [source.id for source in result.sources] == ["native_blog", "rsshub_route"]
    rsshub_source = result.sources[1]
    assert rsshub_source.transport == "rsshub"
    assert rsshub_source.feed.format == "rss"
    assert rsshub_source.url == "https://rsshub.example.com/twitter/user/OpenAI"
    assert rsshub_source.content_class == "official_model_company"


def test_default_registry_keeps_expected_non_x_sources():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    assert "linux_do_top" not in source_by_id
    assert "linux_do_latest" not in source_by_id
    assert source_by_id["linux_do_hot"].default_limit == 12

    producthunt = source_by_id["producthunt_feed"]
    assert producthunt.feed.format == "atom"
    assert producthunt.feed.adapter == "producthunt"
    assert producthunt.content_class == "project_tool"

    release = source_by_id["github_releases_ollama"]
    assert release.github.mode == "releases"
    assert release.source_group == "github_release"
    assert release.content_class == "project_tool"

    assert source_by_id["openrouter_blog"].content_class == "official_model_company"
    assert source_by_id["ithome_ai_news"].content_class == "news_media"
    assert source_by_id["hacker_news_ai"].content_class == "community_social"
    assert source_by_id["ithome_ai_news"].bypass_proxy is True
    assert source_by_id["hacker_news_ai"].bypass_proxy is True


def test_default_registry_contains_verified_x_routes():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    source_by_id = {source.id: source for source in result.sources}

    expected = {
        "x_account_openai_devs": "OpenAIDevs",
        "x_account_opencode": "opencode",
        "x_account_exa_ai_labs": "ExaAILabs",
    }
    for source_id, handle in expected.items():
        source = source_by_id[source_id]
        assert source.transport == "rsshub"
        assert source.source_group == "x_official"
        assert source.content_class == "official_model_company"
        assert source.url == f"https://rsshub.example/twitter/user/{handle}"
        assert source.account_url == f"https://x.com/{handle}"
        assert source.default_limit == 15


@pytest.mark.parametrize(
    ("yaml_body", "error"),
    [
        (
            """
sources:
  - id: legacy
    name: Legacy
    type: rss
    url: https://example.test/feed.xml
""",
            "type",
        ),
        (
            """
sources:
  - id: invalid_github_search
    transport: github
    url: https://api.github.com/search/repositories
    github:
      mode: search
      query: agent
      pushed_days: 0
""",
            "pushed_days",
        ),
        (
            """
sources:
  - id: invalid_producthunt
    transport: feed
    url: https://example.test/producthunt.rss
    feed:
      format: rss
      adapter: producthunt
""",
            "producthunt.*atom",
        ),
        (
            """
sources:
  - id: duplicate
    transport: feed
    url: https://example.test/one.xml
  - id: duplicate
    transport: feed
    url: https://example.test/two.xml
""",
            "duplicate source id: duplicate",
        ),
    ],
)
def test_registry_rejects_invalid_configuration(tmp_path, yaml_body, error):
    path = tmp_path / "source_registry.yaml"
    path.write_text(yaml_body, encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        load_source_registry(path, env={})


def test_enabled_sources_use_only_canonical_groups_and_derived_classes():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    canonical_groups = {
        "official_blog", "official_research", "github_trending", "github_release",
        "github_search", "producthunt", "hacker_news", "reddit_fixed", "reddit_search",
        "linux_do", "x_official", "x_social", "x_search", "tech_media",
    }
    expected_class = {
        "official_blog": "official_model_company",
        "official_research": "official_model_company",
        "x_official": "official_model_company",
        "tech_media": "news_media",
        "github_trending": "project_tool",
        "github_release": "project_tool",
        "github_search": "project_tool",
        "producthunt": "project_tool",
    }

    assert result.sources
    assert all(source.source_group in canonical_groups for source in result.sources)
    assert all(
        source.content_class == expected_class.get(source.source_group, "community_social")
        for source in result.sources
    )


def test_default_registry_physically_removes_rejected_daily_sources():
    registry_path = Path(__file__).parents[1] / "app" / "config" / "source_registry.yaml"
    raw_ids = {
        source["id"]
        for source in yaml.safe_load(registry_path.read_text(encoding="utf-8"))["sources"]
    }
    removed_ids = {
        "aws_whats_new_feed", "aws_machine_learning_blog", "artificial_intelligence_news",
        "marktechpost_ai", "the_verge_ai", "ars_technica_ai", "tomer_tunguz_blog",
        "simon_willison_blog", "interconnects_ai", "bytebytego_ai", "linux_do_top",
        "reddit_local_llama_top_day", "reddit_local_llama_top_week",
        "mistral_news_disabled", "perplexity_updates_disabled",
    }

    assert removed_ids.isdisjoint(raw_ids)


def test_registry_yaml_does_not_reintroduce_removed_classification_fields():
    registry_path = Path(__file__).parents[1] / "app" / "config" / "source_registry.yaml"
    sources = yaml.safe_load(registry_path.read_text(encoding="utf-8"))["sources"]
    removed_fields = {
        "tier", "topic_scopes", "source_subtype", "source_role", "primary_eligible",
        "quality_weight", "spam_risk", "selection_policy", "content_class",
    }

    assert all(removed_fields.isdisjoint(source) for source in sources)


def test_default_registry_applies_every_enabled_source_limit():
    result = load_source_registry(env={"RSSHUB_BASE_URL": "https://rsshub.example"})
    source_by_id = {source.id: source for source in result.sources}
    expected_by_limit = {
        6: {
            "google_research_blog", "microsoft_research_feed", "nvidia_ai_platforms_news",
            "github_blog_ai", "github_releases_claude_code", "github_releases_ollama",
            "github_releases_transformers", "anthropic_research_rsshub",
            "anthropic_engineering_rsshub",
        },
        8: {
            "huggingface_blog", "openrouter_blog", "nvidia_ai_blog", "langchain_blog",
            "google_developers_blog", "producthunt_feed", "x_account_openai",
            "x_account_chatgpt", "x_account_anthropic", "x_account_claude",
            "x_account_google_deepmind", "x_account_gemini", "x_account_xai",
            "x_account_grok", "x_account_ai_at_meta", "x_account_mistral_ai",
            "x_account_cohere", "x_account_perplexity_ai", "x_account_msft_copilot",
            "x_account_deepseek", "x_account_qwen", "x_account_huggingface",
            "x_account_ollama", "x_account_replicate", "x_account_zai_org",
            "x_account_kimi_moonshot", "x_account_minimax_ai", "x_account_baidu",
            "x_account_alibaba_cloud", "x_account_siliconflow", "x_account_openrouter",
            "x_account_runway", "x_account_replit", "x_account_openbmb",
            "x_account_sensetime", "x_account_antling", "x_account_together_ai",
        },
        10: {
            "openai_news", "google_deepmind_blog", "google_blog_ai",
            "deepseek_api_news_rsshub", "anthropic_news_rsshub",
        },
        12: {"cursor_changelog", "hacker_news_ai", "linux_do_hot", "reddit_local_llama_hot"},
        15: {
            "ithome_ai_news", "the_decoder_ai_news", "techcrunch_ai",
            "x_account_claude_devs", "x_account_pvncher", "x_account_thdxr",
            "x_account_xhyctf", "x_account_devin_desktop", "x_account_cursor_ai",
            "x_account_openai_devs", "x_account_opencode", "x_account_exa_ai_labs",
        },
        20: {"x_account_sam_altman", "x_account_tibo"},
    }
    expected_ids = set().union(*expected_by_limit.values())

    assert set(source_by_id) == expected_ids
    for expected_limit, source_ids in expected_by_limit.items():
        assert all(source_by_id[source_id].default_limit == expected_limit for source_id in source_ids)
    assert len(source_by_id) == 69
    assert sum(source.default_limit for source in result.sources) == 668
