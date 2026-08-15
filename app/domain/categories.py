"""Shared editorial topic taxonomy and deterministic legacy fallback."""

from __future__ import annotations

from typing import Sequence


DEFAULT_TOPIC_CATEGORIES: tuple[str, ...] = (
    "模型",
    "产品",
    "行业",
    "论文",
    "教程",
    "观点",
)


def fallback_topic_category(
    *,
    title: str | None,
    summary: str | None,
    content: str | None,
    source_group: str | None,
    source_subtype: str | None,
    transport: str | None,
    content_class: str | None,
    categories: Sequence[str] = DEFAULT_TOPIC_CATEGORIES,
) -> str:
    """Classify old/provider-missing rows without inventing facts."""

    labels = tuple(str(value).strip() for value in categories if str(value).strip())
    text = " ".join(str(part or "") for part in (title, summary, content, source_group, source_subtype)).casefold()
    candidates: list[str] = []
    if source_group == "official_research":
        # The registry already identifies this as a research feed. Prefer the
        # broad editorial bucket for legacy rows; a fresh AI review may still
        # choose a narrower configured topic such as security or governance.
        candidates.append("论文")
    elif any(token in text for token in ("arxiv", "论文", "paper", "benchmark", "研究")):
        candidates.append("论文")
    if transport == "github" or source_group in {"github_trending", "github_search", "github_release"}:
        candidates.extend(["产品", "模型"])
    if source_group == "producthunt":
        candidates.extend(["产品", "模型"])
    if source_group != "official_research" and any(token in text for token in ("安全", "security", "治理", "policy", "隐私", "privacy", "alignment")):
        candidates.insert(0, "行业")
    if content_class == "community_social":
        candidates.append("观点")
    if any(token in text for token in ("应用", "enterprise", "医疗", "金融", "教育", "workflow", "工作流")):
        candidates.append("行业")
    if content_class == "official_model_company":
        candidates.extend(["模型", "产品"])
    for candidate in candidates:
        if candidate in labels:
            return candidate
    return labels[0] if labels else "未分类"
