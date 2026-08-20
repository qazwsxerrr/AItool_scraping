"""Deterministic Stage A/Stage B guards.

Stage A keeps its conservative hard-reject guard.  Stage B only normalizes
source provenance, repairs an empty summary from the frozen input envelope,
and recomputes the local priority score.  Editorial routing, event facts,
paper evidence, and free-form risk judgments are intentionally outside B1.
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import (
    AnalysisResult,
    RawIntelEnvelope,
    ScreenResult,
)


SCORE_WEIGHTS = {
    "relevance": 0.20,
    "importance": 0.20,
    "impact": 0.20,
    "freshness": 0.15,
    "source_authority": 0.10,
    "specificity": 0.10,
    "tracking_value": 0.05,
}


def recompute_analysis_score(result: AnalysisResult) -> int:
    """Calculate the local seven-dimension editorial priority score.

    The provider score is retained in ``raw_response`` for audit, while the
    value used by deterministic Stage-C ordering/gating is reproducible.
    """

    scores = result.score_components
    values = {
        "relevance": scores.relevance,
        "importance": scores.importance or scores.impact,
        "impact": scores.impact,
        "freshness": scores.freshness,
        "source_authority": scores.source_authority,
        "specificity": scores.specificity or scores.relevance,
        "tracking_value": scores.tracking_value or scores.actionability,
    }
    return max(0, min(100, int(round(sum(values[name] * weight for name, weight in SCORE_WEIGHTS.items())))))


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
    """Apply the minimal deterministic Stage-B projection guards."""

    parsed = result if isinstance(result, AnalysisResult) else AnalysisResult.model_validate(result)
    updates: dict[str, Any] = {}
    item = _as_envelope(envelope) if envelope is not None else None

    # The local score is authoritative for downstream ordering.  It is
    # recomputed from the submitted components so the provider cannot return
    # a total that disagrees with its own breakdown.
    recomputed = recompute_analysis_score(parsed)
    if parsed.selection_score != recomputed or parsed.score_components.total != recomputed:
        updates["score_components"] = parsed.score_components.model_copy(update={"total": recomputed})
        updates["selection_score"] = recomputed

    if item is not None:
        if parsed.item_id is None and item.item_id is not None:
            updates["item_id"] = item.item_id
        if parsed.source_content_class is None:
            updates["source_content_class"] = item.source_content_class
        elif parsed.source_content_class != item.source_content_class:
            # Source metadata is authoritative for provenance; provider output
            # cannot relabel a community signal as an official source.
            updates["source_content_class"] = item.source_content_class
        if parsed.source_group is None and item.source_group:
            updates["source_group"] = item.source_group

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
    return parsed.model_copy(update=updates) if updates else parsed


def guard_analysis_result(
    result: AnalysisResult | Mapping[str, Any],
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
) -> AnalysisResult:
    return apply_analysis_guards(result, envelope)


def analysis_guard_failure(result: AnalysisResult | Mapping[str, Any]) -> str | None:
    """Return only a true provider/structural failure reason.

    Editorial value and score are deliberately not Stage-B gates.  An empty
    summary remains structural only after
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
    "SCORE_WEIGHTS",
    "SCREEN_HARD_REJECT_REASON_ALIASES",
    "SCREEN_HARD_REJECT_REASON_CODES",
    "analysis_guard_failure",
    "recompute_analysis_score",
    "apply_analysis_guards",
    "apply_screen_guard",
    "canonical_screen_reason_code",
    "guard_analysis_result",
    "guard_screen_result",
]
