from __future__ import annotations

from dataclasses import asdict, dataclass

from app.ai.verify_client import AIVerifyResponse


HARD_NEGATIVE_FLAGS = {
    "broken_primary_link",
    "fake_open_source_claim",
    "empty_repository",
    "unverifiable_entity",
    "pure_marketing",
    "duplicate_old_news",
    "community_discussion_only",
}


@dataclass(frozen=True)
class FinalVerification:
    verified: bool
    final_keep: bool
    final_score: int
    recommendation_level: str
    relevance_score: int
    usefulness_score: int
    credibility_score: int
    novelty_score: int
    reproducibility_score: int
    audience_fit_score: int
    source_quality_score: int
    spam_risk_score: int
    category: str | None
    summary_cn: str | None
    recommendation_reason: str | None
    risk_reason: str | None
    evidence_summary: list[str]
    risk_flags: list[str]
    raw_response: dict | None


@dataclass(frozen=True)
class EvidenceGuardStats:
    support_evidence_count: int = 0
    contradict_evidence_count: int = 0
    high_confidence_contradict_count: int = 0
    supported_claim_count: int = 0
    contradicted_claim_count: int = 0
    unknown_claim_count: int = 0
    neutral_claim_count: int = 0
    broken_primary_link_count: int = 0
    broken_github_count: int = 0
    broken_huggingface_count: int = 0
    entity_only_support_count: int = 0
    direct_support_count: int = 0

    @property
    def claim_count(self) -> int:
        return (
            self.supported_claim_count
            + self.contradicted_claim_count
            + self.unknown_claim_count
            + self.neutral_claim_count
        )


def finalize_verification(
    response: AIVerifyResponse,
    *,
    evidence_count: int,
    guard_stats: EvidenceGuardStats | None = None,
    min_score: int = 75,
    min_credibility: int = 60,
    max_spam_risk: int = 40,
) -> FinalVerification:
    risk_flags = list(dict.fromkeys(response.risk_flags))
    credibility_score = _clamp(response.credibility_score)
    if evidence_count <= 0:
        credibility_score = min(credibility_score, 50)
        if "weak_evidence" not in risk_flags:
            risk_flags.append("weak_evidence")

    if guard_stats is not None and guard_stats.support_evidence_count == 0:
        credibility_score = min(credibility_score, 50)
        if "no_support_evidence" not in risk_flags:
            risk_flags.append("no_support_evidence")

    relevance_score = _clamp(response.relevance_score)
    usefulness_score = _clamp(response.usefulness_score)
    novelty_score = _clamp(response.novelty_score)
    reproducibility_score = _clamp(response.reproducibility_score)
    audience_fit_score = _clamp(response.audience_fit_score)
    source_quality_score = _clamp(response.source_quality_score)
    spam_risk_score = _clamp(response.spam_risk_score)

    weighted = (
        0.20 * relevance_score
        + 0.20 * usefulness_score
        + 0.20 * credibility_score
        + 0.15 * novelty_score
        + 0.10 * reproducibility_score
        + 0.10 * audience_fit_score
        + 0.05 * source_quality_score
    )
    spam_penalty = max(0, spam_risk_score - 30) * 0.8
    final_score = _clamp(round(weighted - spam_penalty))

    if evidence_count <= 0:
        final_score = min(final_score, 65)

    force_reject = False
    if guard_stats is not None:
        if guard_stats.support_evidence_count == 0:
            final_score = min(final_score, 65)
            force_reject = True
        if guard_stats.high_confidence_contradict_count >= 1:
            final_score = min(final_score, 44)
            force_reject = True
            if "high_confidence_contradiction" not in risk_flags:
                risk_flags.append("high_confidence_contradiction")
        if guard_stats.contradicted_claim_count >= 1:
            final_score = min(final_score, 59)
            force_reject = True
            if "contradicted_claim" not in risk_flags:
                risk_flags.append("contradicted_claim")
        if guard_stats.broken_github_count >= 1 or guard_stats.broken_huggingface_count >= 1:
            final_score = min(final_score, 44)
            force_reject = True
            if "broken_primary_artifact" not in risk_flags:
                risk_flags.append("broken_primary_artifact")
        if guard_stats.claim_count > 0 and guard_stats.supported_claim_count == 0 and guard_stats.contradicted_claim_count == 0:
            final_score = min(final_score, 65)
            if "all_claims_unknown" not in risk_flags:
                risk_flags.append("all_claims_unknown")
        if guard_stats.direct_support_count == 0 and guard_stats.entity_only_support_count > 0:
            credibility_score = min(credibility_score, 60)
            final_score = min(final_score, 70)
            if "entity_only_support" not in risk_flags:
                risk_flags.append("entity_only_support")

    has_hard_negative = bool(HARD_NEGATIVE_FLAGS.intersection(risk_flags))
    if has_hard_negative:
        final_score = min(final_score, 44)

    recommendation_level = level_for_score(final_score)
    final_keep = (
        bool(response.final_keep)
        and final_score >= min_score
        and credibility_score >= min_credibility
        and spam_risk_score <= max_spam_risk
        and evidence_count >= 1
        and not force_reject
        and not has_hard_negative
    )

    raw_response = dict(response.raw_response or {})
    if guard_stats is not None:
        raw_response["guard_stats"] = asdict(guard_stats)

    return FinalVerification(
        verified=bool(response.verified),
        final_keep=final_keep,
        final_score=final_score,
        recommendation_level=recommendation_level,
        relevance_score=relevance_score,
        usefulness_score=usefulness_score,
        credibility_score=credibility_score,
        novelty_score=novelty_score,
        reproducibility_score=reproducibility_score,
        audience_fit_score=audience_fit_score,
        source_quality_score=source_quality_score,
        spam_risk_score=spam_risk_score,
        category=response.category,
        summary_cn=response.summary_cn,
        recommendation_reason=response.recommendation_reason,
        risk_reason=response.risk_reason,
        evidence_summary=response.evidence_summary,
        risk_flags=risk_flags,
        raw_response=raw_response,
    )


def level_for_score(score: int) -> str:
    if score >= 90:
        return "S"
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 45:
        return "C"
    return "D"


def _clamp(value: int | float) -> int:
    return max(0, min(int(value), 100))
