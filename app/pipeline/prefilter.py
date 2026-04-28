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
    raw_text = _raw_text(item).lower()

    core_keywords = sorted(keyword for keyword in CORE_AI_KEYWORDS if keyword.lower() in text)
    supporting_keywords = sorted(keyword for keyword in SUPPORTING_KEYWORDS if keyword.lower() in text)
    matched_keywords = sorted(set(core_keywords + supporting_keywords))
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

    source_group = getattr(getattr(item, "raw_item", None), "source", None)
    source_group_id = getattr(source_group, "id", "") or ""
    if source_group_id.startswith("reddit_local_llama"):
        score += 10
        keep_reasons.append("reddit_local_llama_source")
    elif source_group_id.startswith("linux_do"):
        score += 3
        keep_reasons.append("linux_do_source")
        if len(core_keywords) < 2 and not signal_domains:
            score -= 25
            drop_reasons.append("linux_do_needs_stronger_signal")

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
    keep = score >= 45
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
