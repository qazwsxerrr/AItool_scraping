from datetime import datetime, timezone

from app.config.source_registry import SourceConfig, load_source_registry
from app.jobs.fetch_job import run_fetch_job
from app.parsers.feed_parser import ParsedFeedItem
from app.storage.db import create_engine_from_url, create_session_factory, init_db


class CountingCollector:
    def __init__(self):
        self.calls = []

    def collect(self, source, limit=None):
        self.calls.append((source.id, limit))
        return [
            ParsedFeedItem(
                source_id=source.id,
                external_id=f"{source.id}-{index}",
                title=f"{source.id} item {index}",
                link=f"https://example.com/{source.id}/{index}",
                author=None,
                published_at=datetime(2026, 4, 22, 10, index, tzinfo=timezone.utc),
                raw_summary="summary",
                raw_content="content",
                raw_payload={"index": index},
                content_hash=f"{source.id}-hash-{index}",
            )
            for index in range(limit or 1)
        ]


def make_source(source_id, group, subtype, default_limit):
    return SourceConfig(
        id=source_id,
        name=source_id,
        type="rss",
        url=f"https://example.com/{source_id}.rss",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
        source_group=group,
        source_subtype=subtype,
        default_limit=default_limit,
    )


def test_fetch_job_filters_by_source_group_and_uses_source_default_limit(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'group.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    collector = CountingCollector()

    result = run_fetch_job(
        session_factory=session_factory,
        sources=[
            make_source("linux_do_top", "linux_do", "fixed_top", 15),
            make_source("reddit_new", "reddit_local_llama", "fixed_new", 40),
        ],
        collector=collector,
        source_group_filter="reddit_local_llama",
    )

    assert list(result.stats) == ["reddit_new"]
    assert collector.calls == [("reddit_new", 40)]
    assert result.total_fetched == 40


def test_default_registry_contains_main_site_metadata_and_x_is_env_gated():
    result = load_source_registry(env={})
    source_by_id = {source.id: source for source in result.sources}

    assert source_by_id["linux_do_top"].source_group == "linux_do"
    assert source_by_id["linux_do_top"].source_subtype == "fixed_top"
    assert source_by_id["linux_do_top"].default_limit == 30
    assert source_by_id["linux_do_hot"].source_subtype == "fixed_hot"
    assert source_by_id["linux_do_hot"].default_limit == 30

    assert source_by_id["reddit_local_llama_new"].source_group == "reddit_local_llama"
    assert source_by_id["reddit_local_llama_new"].source_subtype == "fixed_new"
    assert source_by_id["reddit_local_llama_new"].default_limit == 40
    assert source_by_id["reddit_local_llama_search_agent"].source_subtype == "search"
    assert source_by_id["reddit_local_llama_search_agent"].search_query == "agent"
    assert source_by_id["reddit_local_llama_search_mcp"].search_query == "mcp"
    assert source_by_id["reddit_local_llama_search_workflow"].search_query == "workflow"
    assert source_by_id["reddit_local_llama_search_2api"].search_query == "2api"
    assert source_by_id["reddit_local_llama_search_claude_code"].search_query == "claude code"
    assert source_by_id["reddit_local_llama_search_comfyui"].search_query == "comfyui workflow"

    skipped_ids = {item.source_id for item in result.skipped}
    assert "x_account_openai" in skipped_ids
    assert "x_account_anthropic" in skipped_ids
    assert "x_account_deepseek" in skipped_ids
    assert "x_account_mistral_ai" in skipped_ids


def test_x_search_sources_use_rsshub_keyword_routes_when_enabled():
    base_url = "http://127.0.0.1:1200"
    result = load_source_registry(env={"RSSHUB_BASE_URL": base_url})
    source_by_id = {source.id: source for source in result.sources}

    expected_queries = {
        "x_search_github_launch": 'url:github.com ("launch" OR "released" OR "open source") -is:retweet -is:reply',
        "x_search_huggingface_model": 'url:huggingface.co (model OR space OR dataset OR gguf OR weights) -is:retweet -is:reply',
        "x_search_ai_tool_launch": '("AI tool" OR "AI app" OR agent OR workflow) (launch OR released OR introducing OR "open source") -is:retweet -is:reply',
        "x_search_github_ai_tool": 'url:github.com (agent OR LLM OR MCP OR "AI tool" OR "open source") -is:retweet -is:reply',
        "x_search_mcp_agent": '(MCP OR "model context protocol" OR "AI agent" OR "agent workflow") (github OR release OR launch OR tool) -is:retweet -is:reply',
    }

    for source_id, search_query in expected_queries.items():
        source = source_by_id[source_id]
        assert source.type == "rsshub"
        assert source.source_group == "x"
        assert source.source_subtype == "search"
        assert source.default_limit == 20
        assert source.search_query == search_query
        assert source.url.startswith(f"{base_url}/twitter/keyword/")
        assert "/twitter/search/" not in source.url


def test_x_account_sources_track_mainstream_model_official_accounts():
    base_url = "http://127.0.0.1:1200"
    result = load_source_registry(env={"RSSHUB_BASE_URL": base_url})
    source_by_id = {source.id: source for source in result.sources}

    expected_accounts = {
        "x_account_openai": "OpenAI",
        "x_account_chatgpt": "ChatGPTapp",
        "x_account_anthropic": "AnthropicAI",
        "x_account_claude": "claudeai",
        "x_account_google_deepmind": "GoogleDeepMind",
        "x_account_gemini": "GeminiApp",
        "x_account_xai": "xai",
        "x_account_grok": "grok",
        "x_account_ai_at_meta": "AIatMeta",
        "x_account_mistral_ai": "MistralAI",
        "x_account_cohere": "cohere",
        "x_account_perplexity_ai": "perplexity_ai",
        "x_account_msft_copilot": "Copilot",
        "x_account_deepseek": "deepseek_ai",
        "x_account_qwen": "Alibaba_Qwen",
        "x_account_huggingface": "huggingface",
        "x_account_ollama": "ollama",
        "x_account_replicate": "replicate",
        "x_account_sam_altman": "sama",
        "x_account_tibo": "thsottiaux",
        "x_account_together_ai": "togethercompute",
    }

    disabled_or_inactive_accounts = {
        "x_account_meta_ai",
        "x_account_local_llama",
    }

    for source_id, handle in expected_accounts.items():
        source = source_by_id[source_id]
        assert source.type == "rsshub"
        assert source.enabled is True
        assert source.source_group == "x"
        assert source.source_subtype == "account"
        assert source.default_limit == 5
        assert source.url == f"{base_url}/twitter/user/{handle}"
        assert source.search_query == handle

    for source_id in disabled_or_inactive_accounts:
        assert source_id not in source_by_id
