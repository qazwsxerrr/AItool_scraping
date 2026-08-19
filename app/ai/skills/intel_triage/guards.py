"""Deterministic Stage A/Stage B guards.

Provider output is never allowed to upgrade a result.  The screen guard only
preserves high-confidence rejects for an explicit, local hard-reject reason;
all other provider rejects are downgraded to ``uncertain``.  The analysis
guard records paper and empty-summary risks while leaving threshold decisions
to the job.
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


# These are the only provider-supplied reasons that can terminate Stage A.
# Keep the canonical values small and stable; aliases below are accepted for
# rolling compatibility with older prompts/providers and are never trusted as
# a separate policy surface.
SCREEN_HARD_REJECT_REASON_CODES = frozenset(
    {
        "irrelevant",
        "spam",
        "pure_advertisement",
        "navigation_or_index",
        "empty_content",
        "duplicate_without_update",
    }
)
SCREEN_HARD_REJECT_REASON_ALIASES = {
    "not_relevant": "irrelevant",
    "unrelated": "irrelevant",
    "off_topic": "irrelevant",
    "noise": "spam",
    "ad": "pure_advertisement",
    "advertisement": "pure_advertisement",
    "marketing_only": "pure_advertisement",
    "promotional": "pure_advertisement",
    "navigation": "navigation_or_index",
    "index_page": "navigation_or_index",
    "empty": "empty_content",
    "no_content": "empty_content",
    "duplicate": "duplicate_without_update",
    "repost": "duplicate_without_update",
    "no_new_information": "duplicate_without_update",
}


def canonical_screen_reason_code(value: Any) -> str:
    """Normalize a provider reason code for deterministic local policy."""

    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _screen_hard_reject_reason(value: Any) -> str | None:
    normalized = canonical_screen_reason_code(value)
    canonical = SCREEN_HARD_REJECT_REASON_ALIASES.get(normalized, normalized)
    return canonical if canonical in SCREEN_HARD_REJECT_REASON_CODES else None


def _as_envelope(value: RawIntelEnvelope | Mapping[str, Any] | Any) -> RawIntelEnvelope:
    return value if isinstance(value, RawIntelEnvelope) else RawIntelEnvelope.model_validate(value)


def apply_screen_guard(
    result: ScreenResult | Mapping[str, Any],
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    *,
    reject_threshold: int = 90,
) -> ScreenResult:
    """Normalize Stage A output without allowing an unsafe rejection.

    A provider reject is actionable only when its reason code belongs to the
    local hard-reject allowlist and its confidence reaches ``reject_threshold``.
    Lower-confidence or unknown-reason rejects become ``uncertain`` and
    therefore continue to Stage B. ``screen_failed`` records are never
    converted into a successful decision.
    """

    parsed = result if isinstance(result, ScreenResult) else ScreenResult.model_validate(result)
    threshold = max(0, min(int(reject_threshold), 100))
    updates: dict[str, Any] = {}
    if parsed.status == "success" and parsed.decision == "reject":
        hard_reason = _screen_hard_reject_reason(parsed.reason_code)
        flags = list(parsed.risk_flags)
        if hard_reason is None:
            updates["decision"] = "uncertain"
            if "screen:non_hard_reject_reason" not in flags:
                flags.append("screen:non_hard_reject_reason")
        elif parsed.confidence < threshold:
            updates["decision"] = "uncertain"
            if "screen:low_confidence_reject" not in flags:
                flags.append("screen:low_confidence_reject")
        if flags != parsed.risk_flags:
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
    reject_threshold: int = 90,
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
        # A provider may omit the generated summary even though the frozen
        # source envelope still has useful text.  Preserve that source text as
        # a conservative fallback so Stage B does not discard an otherwise
        # structurally valid item.  The fallback is explicitly auditable and
        # never invents content beyond the source summary/title.
        fallback = ""
        if item is not None:
            fallback = str(item.summary or item.title or "").strip()
        if fallback:
            updates["summary_cn"] = fallback
            flags = [flag for flag in flags if flag != "summary:empty"]
            if "summary:fallback" not in flags:
                flags.append("summary:fallback")
        elif "summary:empty" not in flags:
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
    """Return only a true provider/structural failure reason.

    Editorial value, score and paper support are deliberately not Stage-B
    gates.  An empty summary remains structural only after
    :func:`apply_analysis_guards` had a chance to recover the source summary or
    title as a fallback.
    """

    parsed = result if isinstance(result, AnalysisResult) else AnalysisResult.model_validate(result)
    if parsed.status != "success":
        return "analysis_failed"
    if not parsed.summary_cn.strip():
        return "summary_empty"
    return None


__all__ = [
    "SCREEN_HARD_REJECT_REASON_ALIASES",
    "SCREEN_HARD_REJECT_REASON_CODES",
    "analysis_guard_failure",
    "apply_analysis_guards",
    "apply_screen_guard",
    "canonical_screen_reason_code",
    "guard_analysis_result",
    "guard_paper_support",
    "guard_screen_result",
]
