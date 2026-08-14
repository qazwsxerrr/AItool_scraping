"""Deterministic safety rules applied after provider parsing."""

from __future__ import annotations

from typing import Any, Mapping

from .models import (
    COMMUNITY_SOCIAL,
    CONTENT_CLASS_TO_DEFAULT_TOPIC,
    OFFICIAL_MODEL_COMPANY,
    PROJECT_TOOL,
    TOPIC_INDUSTRY,
    TOPIC_MODEL,
    TOPIC_OPINION,
    TOPIC_PAPER,
    TOPIC_PRODUCT,
    TOPIC_PROJECT,
    TOPIC_TUTORIAL,
    RawIntelEnvelope,
    TriageResult,
    normalize_topic,
)


def infer_topic(envelope: RawIntelEnvelope | Mapping[str, Any] | Any) -> str:
    """Infer a safe fallback topic from source class and visible text only."""

    item = envelope if isinstance(envelope, RawIntelEnvelope) else RawIntelEnvelope.model_validate(envelope)
    text = f"{item.title} {item.summary or ''} {item.body_text or ''}".casefold()
    if any(token in text for token in ("arxiv", "paper", "论文", "研究", "preprint", "benchmark")):
        return TOPIC_PAPER
    if any(token in text for token in ("教程", "tutorial", "guide", "how to", "how-to", "入门")):
        return TOPIC_TUTORIAL
    if any(token in text for token in ("观点", "评论", "opinion", "analysis", "评论文章")):
        return TOPIC_OPINION
    if any(token in text for token in ("行业", "industry", "market", "融资", "regulation", "政策")):
        return TOPIC_INDUSTRY
    if any(token in text for token in ("model", "模型", "llm", "checkpoint", "weights", "权重")):
        return TOPIC_MODEL
    if any(token in text for token in ("github", "repo", "repository", "开源", "项目", "agent", "mcp")):
        return TOPIC_PROJECT
    if item.source_content_class == PROJECT_TOOL:
        return TOPIC_PROJECT
    if item.source_content_class == OFFICIAL_MODEL_COMPANY:
        return TOPIC_PRODUCT
    if item.source_content_class == COMMUNITY_SOCIAL:
        return TOPIC_OPINION
    return TOPIC_PRODUCT


def guard_paper_support(result: TriageResult) -> TriageResult:
    """Apply the paper hard gate without ever upgrading a result.

    arXiv-only papers are always rejected.  Other papers require an explicit
    supported/strong paper support record and at least one link/code/official
    source field.  The guard only lowers ``keep`` and appends audit flags.
    """

    if result.topic != TOPIC_PAPER:
        return result
    support = result.paper_support
    flags = list(result.risk_flags)
    updates: dict[str, Any] = {}
    if not support.is_paper:
        if "paper:not_declared" not in flags:
            flags.append("paper:not_declared")
        updates["keep"] = False
    elif support.arxiv_only:
        if "paper:arxiv_only" not in flags:
            flags.append("paper:arxiv_only")
        updates["keep"] = False
    elif not support.hard_gate_pass:
        if "paper:unsupported" not in flags:
            flags.append("paper:unsupported")
        updates["keep"] = False
    if flags != result.risk_flags:
        updates["risk_flags"] = flags
    return result.model_copy(update=updates) if updates else result


def apply_deterministic_guards(
    result: TriageResult,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
) -> TriageResult:
    """Apply all local guards while preserving raw provider data.

    Source metadata is authoritative for ``content_class``.  The function is
    monotonic: it can reject or lower confidence/score, but never upgrades a
    provider result.
    """

    if not isinstance(result, TriageResult):
        result = TriageResult.model_validate(result)
    item = None
    if envelope is not None:
        item = envelope if isinstance(envelope, RawIntelEnvelope) else RawIntelEnvelope.model_validate(envelope)

    updates: dict[str, Any] = {}
    flags = list(result.risk_flags)
    if item is not None:
        if result.item_id is None and item.item_id is not None:
            updates["item_id"] = item.item_id
        if result.content_class != item.source_content_class:
            # A provider cannot turn a community discovery source into an
            # official source (or vice versa).
            updates["content_class"] = item.source_content_class
        if result.source_group is None and item.source_group:
            updates["source_group"] = item.source_group

        # An arXiv URL in the source envelope is enough to mark an otherwise
        # incomplete paper record as arXiv-only; it still cannot pass the gate.
        if result.topic == TOPIC_PAPER and item.url and "arxiv.org" in item.url.casefold():
            support = result.paper_support
            if not support.paper_url or support.paper_url != item.url:
                support = support.model_copy(update={"paper_url": item.url, "is_paper": True, "arxiv_only": True})
                updates["paper_support"] = support

    if result.keep and not result.summary_cn.strip():
        updates["keep"] = False
        if "summary:empty" not in flags:
            flags.append("summary:empty")

    if result.topic not in {
        TOPIC_MODEL,
        TOPIC_PRODUCT,
        TOPIC_PROJECT,
        TOPIC_INDUSTRY,
        TOPIC_TUTORIAL,
        TOPIC_OPINION,
        TOPIC_PAPER,
    }:
        updates["topic"] = TOPIC_OPINION
        updates["topics"] = [TOPIC_OPINION]
        if "topic:invalid" not in flags:
            flags.append("topic:invalid")

    # Community/social items remain discovery signals.  Keep the model's
    # decision but make the weaker provenance explicit for downstream ranking.
    final_class = updates.get("content_class", result.content_class)
    if final_class == COMMUNITY_SOCIAL and "source:social_only" not in flags:
        flags.append("source:social_only")

    if flags != result.risk_flags:
        updates["risk_flags"] = flags
    guarded = result.model_copy(update=updates) if updates else result
    return guard_paper_support(guarded)


guard_triage_result = apply_deterministic_guards


__all__ = [
    "apply_deterministic_guards",
    "guard_paper_support",
    "guard_triage_result",
    "infer_topic",
]
