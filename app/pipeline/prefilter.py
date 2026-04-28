from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.storage.models import NormalizedItem

CORE_AI_KEYWORDS = {
    "agent",
    "workflow",
    "automation",
    "automate",
    "model",
    "models",
    "llm",
    "llama",
    "local llm",
    "open weights",
    "open source",
    "opensource",
    "gguf",
    "benchmark",
    "inference",
    "quantization",
    "rag",
    "release",
    "released",
    "launch",
    "launched",
    "claude",
    "gpt",
    "gemini",
    "deepseek",
    "kimi",
    "qwen",
    "mistral",
    "模型",
    "自动化",
    "工作流",
    "智能体",
    "提示词",
    "部署",
    "量化",
}

TARGET_TOOL_KEYWORDS = {
    "mcp",
    "skill",
    "skills",
    "workflow",
    "2api",
    "api proxy",
    "openai-compatible",
    "openai compatible",
    "reverse proxy",
    "proxy",
    "router",
    "gateway",
    "agent workflow",
    "agentic workflow",
    "open webui",
    "new api",
    "one-api",
    "litellm",
    "dify",
    "coze",
    "automation",
    "工作流",
    "智能体",
    "反代",
    "中转",
    "转发",
    "网关",
    "路由",
    "编排",
    "部署教程",
}

TARGET_PROGRESS_KEYWORDS = {
    "released",
    "launch",
    "launched",
    "open weights",
    "model release",
    "new model",
    "announcing",
    "presents",
    "open-sourced",
    "opensourced",
    "fine-tune",
    "finetune",
    "发布",
    "上线",
    "新模型",
    "开源权重",
    "微调",
}

GENERIC_DISCUSSION_KEYWORDS = {
    "benchmark",
    "benchmarks",
    "comparison",
    "compared",
    "opinion",
    "opinions",
    "question",
    "asking for",
    "are there any",
    "has anyone",
    "thoughts",
    "i'm done",
    "i am done",
    "discussion",
    "productivity",
    "threshold",
    "feasible for real work",
    "terminal-bench",
    "power-limit",
    "power limit",
    "tg/s",
    "vram users",
    "class of 2026",
    "tune",
    "tuning",
    "speed benchmarks",
    "token speed",
    "对比",
    "比较",
    "观点",
    "讨论",
    "吐槽",
    "感想",
}

PERSONAL_EXPERIENCE_OR_QUESTION_KEYWORDS = {
    "i'm done",
    "i am done",
    "my verdict",
    "i used",
    "i tried",
    "i’ve been using",
    "i've been using",
    "for a few weeks",
    "curious if",
    "are there any",
    "has anyone",
    "question about",
    "loss of productivity",
    "bottleneck",
    "disk contention",
    "rebooting",
    "funding",
    "investment",
    "raised",
    "融资",
    "投资",
    "红包",
    "感恩",
    "亲历",
    "故事",
    "个人经历",
    "有没有",
    "求推荐",
}

FUNDING_STORY_KEYWORDS = {
    "funding",
    "investment",
    "raised",
    "融资",
    "投资",
    "红包",
    "感恩",
    "亲历",
}

EXPLICIT_ACTIONABLE_OR_RELEASE_KEYWORDS = {
    "how to",
    "tutorial",
    "guide",
    "step-by-step",
    "setup guide",
    "released",
    "launch",
    "announcing",
    "presents",
    "open-sourced",
    "opensourced",
    "github repo",
    "mcp server",
    "mcp client",
    "2api",
    "api proxy",
    "reverse proxy",
    "openai-compatible",
    "open webui",
    "new api",
    "教程",
    "教学",
    "指南",
    "发布",
    "上线",
    "开源项目",
    "反代",
    "中转",
}

SUPPORTING_KEYWORDS = {
    "ai",
    "tool",
    "tools",
    "github",
    "huggingface",
    "demo",
    "waitlist",
    "open source",
    "opensource",
    "release",
    "released",
    "launch",
    "launched",
    "开源",
    "工具",
    "上线",
    "发布",
    "更新",
}

NOISE_KEYWORDS = {
    "meme",
    "shitpost",
    "闲聊",
    "水贴",
    "吃什么",
    "抽奖",
}

SIGNAL_DOMAINS = {
    "github.com": "github_link",
    "huggingface.co": "huggingface_link",
    "producthunt.com": "producthunt_link",
}


@dataclass(frozen=True)
class CandidateDecision:
    keep: bool
    score: int
    matched_keywords: list[str] = field(default_factory=list)
    keep_reasons: list[str] = field(default_factory=list)
    drop_reasons: list[str] = field(default_factory=list)


def evaluate_candidate(item: NormalizedItem) -> CandidateDecision:
    """Rule pre-filter before AI analysis. Conservative keep for strong tool/model signals."""
    title = item.title or ""
    body = item.body_text or ""
    url = item.url or ""
    text = f"{title}\n{body}\n{url}".lower()
    title_text = title.lower()
    raw_text = _raw_text(item).lower()

    core_keywords = sorted(keyword for keyword in CORE_AI_KEYWORDS if keyword.lower() in text)
    supporting_keywords = sorted(keyword for keyword in SUPPORTING_KEYWORDS if keyword.lower() in text)
    target_tool_keywords = sorted(keyword for keyword in TARGET_TOOL_KEYWORDS if keyword.lower() in raw_text)
    target_progress_keywords = sorted(keyword for keyword in TARGET_PROGRESS_KEYWORDS if keyword.lower() in title_text)
    generic_discussion_keywords = sorted(keyword for keyword in GENERIC_DISCUSSION_KEYWORDS if keyword.lower() in raw_text)
    personal_or_question_keywords = sorted(
        keyword for keyword in PERSONAL_EXPERIENCE_OR_QUESTION_KEYWORDS if keyword.lower() in raw_text
    )
    funding_story_keywords = sorted(keyword for keyword in FUNDING_STORY_KEYWORDS if keyword.lower() in raw_text)
    explicit_actionable_or_release_keywords = sorted(
        keyword for keyword in EXPLICIT_ACTIONABLE_OR_RELEASE_KEYWORDS if keyword.lower() in raw_text
    )
    explicit_title_keywords = sorted(
        keyword for keyword in EXPLICIT_ACTIONABLE_OR_RELEASE_KEYWORDS if keyword.lower() in title_text
    )
    matched_keywords = sorted(set(core_keywords + supporting_keywords + target_tool_keywords + target_progress_keywords))
    noise_keywords = sorted(keyword for keyword in NOISE_KEYWORDS if keyword.lower() in text)
    keep_reasons: list[str] = []
    drop_reasons: list[str] = []
    score = 0

    score += min(len(core_keywords) * 14, 56)
    score += min(len(supporting_keywords) * 5, 20)
    if matched_keywords:
        keep_reasons.append("keyword_signal")

    signal_domains = [domain for domain in SIGNAL_DOMAINS if domain in raw_text]
    if signal_domains:
        score += 20
        keep_reasons.append("external_link_signal")

    for domain in signal_domains:
        reason = SIGNAL_DOMAINS[domain]
        score += 15
        keep_reasons.append(reason)

    has_repo_release_signal = bool(
        signal_domains
        and any(keyword in raw_text for keyword in ["released", "release", "launch", "launched", "发布", "上线"])
        and not generic_discussion_keywords
    )
    has_target_signal = bool(target_tool_keywords or target_progress_keywords or has_repo_release_signal)
    if target_tool_keywords:
        score += 25
        keep_reasons.append("target_tool_signal")
    if target_progress_keywords or has_repo_release_signal:
        score += 15
        keep_reasons.append("target_progress_signal")

    source_group = getattr(getattr(item, "raw_item", None), "source", None)
    source_group_id = getattr(source_group, "id", "") or ""
    if source_group_id.startswith("reddit_local_llama"):
        if has_target_signal:
            score += 10
            keep_reasons.append("reddit_local_llama_source")
    elif source_group_id.startswith("linux_do"):
        score += 3
        keep_reasons.append("linux_do_source")
        if len(core_keywords) < 2 and not signal_domains:
            score -= 25
            drop_reasons.append("linux_do_needs_stronger_signal")

    if generic_discussion_keywords and not target_tool_keywords and not target_progress_keywords:
        score -= 45
        drop_reasons.append("generic_benchmark_or_opinion")
    elif generic_discussion_keywords and not (target_tool_keywords or signal_domains):
        score -= 25
        drop_reasons.append("generic_benchmark_or_opinion")

    if personal_or_question_keywords and not explicit_title_keywords:
        score -= 70
        drop_reasons.append("personal_experience_or_question")
    elif personal_or_question_keywords and not (target_progress_keywords or signal_domains):
        score -= 45
        drop_reasons.append("personal_experience_or_question")

    if funding_story_keywords:
        score -= 80
        drop_reasons.append("funding_story")

    if noise_keywords:
        score -= min(len(noise_keywords) * 15, 30)
        drop_reasons.append("noise_keyword")

    if not title.strip():
        score -= 30
        drop_reasons.append("missing_title")
    if not item.url and not body.strip():
        score -= 30
        drop_reasons.append("missing_link_and_body")

    score = max(0, min(score, 100))
    hard_drop = ("personal_experience_or_question" in drop_reasons and not explicit_title_keywords) or (
        "funding_story" in drop_reasons
    )
    keep = score >= 55 and has_target_signal and not hard_drop
    if not keep:
        drop_reasons.append("low_score")

    return CandidateDecision(
        keep=keep,
        score=score,
        matched_keywords=matched_keywords,
        keep_reasons=keep_reasons,
        drop_reasons=drop_reasons,
    )


def _raw_text(item: NormalizedItem) -> str:
    raw_item = getattr(item, "raw_item", None)
    parts = [
        item.title or "",
        item.body_text or "",
        item.url or "",
    ]
    if raw_item is not None:
        parts.extend(
            [
                raw_item.raw_summary or "",
                raw_item.raw_content or "",
            ]
        )
    return "\n".join(parts)
