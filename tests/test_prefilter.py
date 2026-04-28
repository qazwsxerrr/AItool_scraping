from datetime import datetime, timezone

from app.pipeline.prefilter import evaluate_candidate
from app.storage.models import NormalizedItem, RawItem, Source


def make_normalized_item(
    source_id="reddit_local_llama_new",
    title="New GGUF model released",
    body_text="Open weights model with GitHub repo",
    url="https://github.com/example/model",
    raw_html: str | None = None,
):
    source = Source(
        id=source_id,
        name=source_id,
        type="atom",
        url="https://example.com/feed",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
    )
    raw_item = RawItem(
        id=1,
        source_id=source_id,
        external_id="guid-1",
        title=title,
        link=url,
        author="author",
        published_at=datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
        raw_summary=raw_html or body_text,
        raw_content=raw_html or body_text,
        raw_payload="{}",
        content_hash="hash-1",
        status="normalized",
        source=source,
    )
    return NormalizedItem(
        id=1,
        raw_item_id=1,
        title=title,
        body_text=body_text,
        url=url,
        author="author",
        published_at=datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
        language="en",
        dedupe_key=f"url:{url}",
        raw_item=raw_item,
    )


def test_evaluate_candidate_keeps_ai_tool_release_with_link():
    decision = evaluate_candidate(make_normalized_item())

    assert decision.keep is True
    assert decision.score >= 60
    assert "gguf" in decision.matched_keywords
    assert "github_link" in decision.keep_reasons


def test_evaluate_candidate_drops_low_signal_chatter():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_hot",
            title="大家今天吃什么",
            body_text="随便聊聊天，没有工具，没有模型，也没有链接",
            url=None,
        )
    )

    assert decision.keep is False
    assert "low_score" in decision.drop_reasons


def test_evaluate_candidate_drops_linux_do_community_notice_with_only_generic_keywords():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_hot",
            title="请不要把互联网上的戾气带来这里！",
            body_text="社区公告，工具更新与发布规则说明，欢迎友善交流。",
            url="https://linux.do/t/topic/482293",
        )
    )

    assert decision.keep is False
    assert "low_score" in decision.drop_reasons


def test_evaluate_candidate_detects_external_repo_signal_from_linux_do_raw_html():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_top",
            title="开源智能体工作流工具发布",
            body_text="项目支持本地模型编排。",
            url="https://linux.do/t/topic/123456",
            raw_html=(
                '<p>项目支持本地模型编排。</p>'
                '<a href="https://github.com/example/agent-workflow">GitHub repo</a>'
            ),
        )
    )

    assert decision.keep is True
    assert "external_link_signal" in decision.keep_reasons


def test_evaluate_candidate_drops_generic_benchmark_without_tool_or_release_signal():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_search_benchmark",
            title="The 4B class of 2026 benchmark",
            body_text="I compared several models on finance reasoning and code tasks. Here are benchmark tables and opinions.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/benchmark",
        )
    )

    assert decision.keep is False
    assert "generic_benchmark_or_opinion" in decision.drop_reasons


def test_evaluate_candidate_drops_local_llm_opinion_without_tool_release_signal():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_hot",
            title="I'm done with using local LLMs for coding",
            body_text="I used local models for coding and think productivity is worse than Claude Code. This is my opinion.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/opinion",
        )
    )

    assert decision.keep is False
    assert "generic_benchmark_or_opinion" in decision.drop_reasons


def test_evaluate_candidate_keeps_mcp_workflow_or_2api_tool_signal():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_top",
            title="开源 MCP 工作流工具发布，支持 OpenAI 2API 反代",
            body_text="这个工具提供 MCP server、skill 编排、反代到 OpenAI-compatible API，并包含部署教程。",
            url="https://linux.do/t/topic/tool",
            raw_html='<a href="https://github.com/example/mcp-2api-proxy">GitHub repo</a>',
        )
    )

    assert decision.keep is True
    assert "target_tool_signal" in decision.keep_reasons


def test_evaluate_candidate_drops_generic_benchmark_even_with_github_link():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_search_open_weights",
            title="The 4B class of 2026 (benchmark)",
            body_text="Benchmark of released models with tables and a GitHub repo containing the scripts.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/benchmark",
            raw_html='<a href="https://github.com/example/bench-scripts">GitHub repo</a>',
        )
    )

    assert decision.keep is False
    assert "generic_benchmark_or_opinion" in decision.drop_reasons


def test_evaluate_candidate_drops_power_tuning_even_with_tool_config_text():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_search_benchmark",
            title="Power-limit vs TG/s for 2x3090",
            body_text="Trying to tune VLLM server config with --enable-auto-tool-choice and benchmark token speed.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/power",
            raw_html='<a href="https://github.com/example/config">GitHub config</a>',
        )
    )

    assert decision.keep is False
    assert "generic_benchmark_or_opinion" in decision.drop_reasons


def test_evaluate_candidate_keeps_explicit_new_model_release():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_top_day",
            title="Qwen3.6-35B-A3B released with open weights",
            body_text="New model release. Weights are available on Hugging Face.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/model-release",
            raw_html='<a href="https://huggingface.co/example/qwen">Hugging Face model</a>',
        )
    )

    assert decision.keep is True
    assert "target_progress_signal" in decision.keep_reasons


def test_evaluate_candidate_drops_coding_agent_question_without_specific_tool_release():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_new",
            title="Apart from Claude Code, are there any coding agents that can run fully autonomously without requiring interaction?",
            body_text="I am asking for recommendations and discussion about coding agents.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/question",
        )
    )

    assert decision.keep is False
    assert "generic_benchmark_or_opinion" in decision.drop_reasons


def test_evaluate_candidate_drops_local_model_coding_threshold_discussion():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_search_open_weights",
            title="Local model on coding has reached a certain threshold to be feasible for real work",
            body_text="We ran open-weight models on Terminal-Bench and compared numbers to proprietary models.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/threshold",
            raw_html='<a href="https://huggingface.co/example/model">HF model</a>',
        )
    )

    assert decision.keep is False
    assert "generic_benchmark_or_opinion" in decision.drop_reasons


def test_evaluate_candidate_drops_local_llm_game_question_with_full_release_phrase():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_new",
            title="Has anyone made a decent Zorklike text game that runs on local LLM? Like a full release",
            body_text="Question about whether a game exists.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/game",
        )
    )

    assert decision.keep is False


def test_evaluate_candidate_drops_personal_local_llm_coding_verdict_even_with_agentic_apps():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_hot",
            title="I'm done with using local LLMs for coding",
            body_text=(
                "I used Qwen and Gemma with multiple agentic apps. My verdict is that "
                "the loss of productivity is not worth it. This is a discussion of issues."
            ),
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/verdict",
        )
    )

    assert decision.keep is False
    assert "personal_experience_or_question" in decision.drop_reasons


def test_evaluate_candidate_drops_question_about_coding_agents_even_with_claude_headless_workflow():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_new",
            title="Apart from Claude Code, are there any coding agents that can run fully autonomously without requiring interaction?",
            body_text=(
                "I’ve been using claude code -p to execute workflows inside an agent. "
                "Curious if others have found similar tools or setups."
            ),
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/question",
        )
    )

    assert decision.keep is False
    assert "personal_experience_or_question" in decision.drop_reasons


def test_evaluate_candidate_drops_personal_deployment_case_study_even_with_litellm_proxy():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_new",
            title="Qwen 35B-A3B as an always-on agentic loop on a 16GB Mac M4: disk became the bottleneck before RAM",
            body_text=(
                "For a few weeks I had Qwen running under llama.cpp. Stack included "
                "LiteLLM bridge proxying everything as a Claude-compatible endpoint. "
                "The interesting part is disk contention and rebooting."
            ),
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/case-study",
        )
    )

    assert decision.keep is False
    assert "personal_experience_or_question" in decision.drop_reasons


def test_evaluate_candidate_drops_funding_story_even_if_open_source_project_is_mentioned():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_top",
            title="From open-source project hype to funding investment: my personal Vibe Coding story",
            body_text="Funding story and personal experience, not a concrete reusable AI tool release or tutorial.",
            url="https://linux.do/t/topic/story",
            raw_html='<a href="https://github.com/example/project">GitHub repo</a>',
        )
    )

    assert decision.keep is False
    assert "personal_experience_or_question" in decision.drop_reasons


def test_evaluate_candidate_keeps_n8n_ai_workflow_template():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_search_workflow",
            title="Released n8n AI agent workflow template for triage automation",
            body_text="Reusable workflow with OpenAI-compatible model routing and deployment guide.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/n8n-workflow",
            raw_html='<a href="https://github.com/example/n8n-ai-workflow">GitHub repo</a>',
        )
    )

    assert decision.keep is True
    assert "target_tool_signal" in decision.keep_reasons


def test_evaluate_candidate_keeps_comfyui_workflow_release():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_search_workflow",
            title="Open-sourced ComfyUI workflow for image-to-video agent pipeline",
            body_text="Includes reusable nodes, prompts, and setup guide.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/comfyui",
            raw_html='<a href="https://github.com/example/comfyui-workflow">GitHub repo</a>',
        )
    )

    assert decision.keep is True
    assert "target_tool_signal" in decision.keep_reasons


def test_evaluate_candidate_keeps_claude_code_codex_workflow_release():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="reddit_local_llama_search_claude_code",
            title="Released Claude Code + Codex workflow for autonomous repo maintenance",
            body_text="Reusable workflow template with skills and MCP server integration.",
            url="https://www.reddit.com/r/LocalLLaMA/comments/example/claude-codex",
            raw_html='<a href="https://github.com/example/claude-codex-workflow">GitHub repo</a>',
        )
    )

    assert decision.keep is True
    assert "target_tool_signal" in decision.keep_reasons
