"""Deterministic Stage A/Stage B guards.

Provider output is never allowed to upgrade a result.  The screen guard only
normalizes low-confidence rejects to ``uncertain``; the analysis guard records
paper and empty-summary risks while leaving threshold decisions to the job.
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import (
    AnalysisResult,
    COMMUNITY_SOCIAL,
    RawIntelEnvelope,
    ScreenResult,
    TOPIC_PAPER,
)


def _as_envelope(value: RawIntelEnvelope | Mapping[str, Any] | Any) -> RawIntelEnvelope:
    return value if isinstance(value, RawIntelEnvelope) else RawIntelEnvelope.model_validate(value)


def apply_screen_guard(
    result: ScreenResult | Mapping[str, Any],
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    *,
    reject_threshold: int = 85,
) -> ScreenResult:
    """Normalize Stage A output without allowing an unsafe rejection.

    A reject is actionable only at or above ``reject_threshold``.  Lower
    confidence rejects become ``uncertain`` and therefore continue to Stage B.
    ``screen_failed`` records are never converted into a successful decision.
    """

    parsed = result if isinstance(result, ScreenResult) else ScreenResult.model_validate(result)
    threshold = max(0, min(int(reject_threshold), 100))
    updates: dict[str, Any] = {}
    if parsed.status == "success" and parsed.decision == "reject" and parsed.confidence < threshold:
        updates["decision"] = "uncertain"
        flags = list(parsed.risk_flags)
        if "screen:low_confidence_reject" not in flags:
            flags.append("screen:low_confidence_reject")
        updates["risk_flags"] = flags
    if envelope is not None:
        item = _as_envelope(envelope)
        if parsed.item_id is None and item.item_id is not None:
            updates["item_id"] = item.item_id
    return parsed.model_copy(update=updates) if updates else parsed


def guard_screen_result(
    result: ScreenResult | Mapping[str, Any],
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    *,
    reject_threshold: int = 85,
) -> ScreenResult:
    """Descriptive alias used by pipeline callers."""

    return apply_screen_guard(result, envelope, reject_threshold=reject_threshold)


def apply_analysis_guards(
    result: AnalysisResult | Mapping[str, Any],
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
) -> AnalysisResult:
    """Apply monotonic Stage B safety guards and preserve raw provider data."""

    parsed = result if isinstance(result, AnalysisResult) else AnalysisResult.model_validate(result)
    updates: dict[str, Any] = {}
    flags = list(parsed.risk_flags)
    item = _as_envelope(envelope) if envelope is not None else None

    if item is not None:
        if parsed.item_id is None and item.item_id is not None:
            updates["item_id"] = item.item_id
        if parsed.source_content_class is None:
            updates["source_content_class"] = item.source_content_class
        elif parsed.source_content_class != item.source_content_class:
            # Source metadata is authoritative for provenance; provider output
            # cannot relabel a community signal as an official source.
            updates["source_content_class"] = item.source_content_class
            if "source:class_mismatch" not in flags:
                flags.append("source:class_mismatch")
        if parsed.source_group is None and item.source_group:
            updates["source_group"] = item.source_group

        if parsed.topic == TOPIC_PAPER and item.url and "arxiv.org" in item.url.casefold():
            support = parsed.paper_support
            if support.paper_url != item.url or not support.is_paper:
                support = support.model_copy(update={"paper_url": item.url, "is_paper": True, "arxiv_only": True})
                updates["paper_support"] = support

    if parsed.status == "success" and not parsed.summary_cn.strip():
        if "summary:empty" not in flags:
            flags.append("summary:empty")

    if parsed.topic == TOPIC_PAPER and not parsed.paper_support.hard_gate_pass:
        if parsed.paper_support.arxiv_only and "paper:arxiv_only" not in flags:
            flags.append("paper:arxiv_only")
        elif "paper:unsupported" not in flags:
            flags.append("paper:unsupported")

    if parsed.source_content_class == COMMUNITY_SOCIAL and "source:social_only" not in flags:
        flags.append("source:social_only")
    if flags != parsed.risk_flags:
        updates["risk_flags"] = flags
    return parsed.model_copy(update=updates) if updates else parsed


def guard_analysis_result(
    result: AnalysisResult | Mapping[str, Any],
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
) -> AnalysisResult:
    return apply_analysis_guards(result, envelope)


def guard_paper_support(result: AnalysisResult | Mapping[str, Any]) -> AnalysisResult:
    """Apply the paper-only guard without requiring the raw envelope."""

    return apply_analysis_guards(result)


def analysis_guard_failure(result: AnalysisResult | Mapping[str, Any]) -> str | None:
    """Return a deterministic Stage B filter reason, if one applies."""

    parsed = result if isinstance(result, AnalysisResult) else AnalysisResult.model_validate(result)
    if parsed.status != "success":
        return "analysis_failed"
    if not parsed.summary_cn.strip():
        return "summary_empty"
    if parsed.topic == TOPIC_PAPER and not parsed.paper_support.hard_gate_pass:
        return "paper_support_failed"
    return None


__all__ = [
    "analysis_guard_failure",
    "apply_analysis_guards",
    "apply_screen_guard",
    "guard_analysis_result",
    "guard_paper_support",
    "guard_screen_result",
]
