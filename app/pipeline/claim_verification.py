from __future__ import annotations

import json
from dataclasses import dataclass

from app.storage.models import EvidenceItem, ExtractedClaim


@dataclass(frozen=True)
class ClaimVerificationDecision:
    claim_index: int
    claim_text: str
    supports_claim: str
    evidence_item_ids: list[int]
    confidence: int
    risk_flags: list[str]
    raw_response: dict


def verify_claims_for_extracted_claim(claim: ExtractedClaim) -> list[ClaimVerificationDecision]:
    """Run a deterministic claim-level evidence check.

    This is intentionally conservative: Tavily discovery alone is not enough;
    only classified evidence with support/contradict labels can strongly affect
    claim confidence.  The AI verify layer receives these per-claim records as
    grounded hints rather than raw search-result relevance scores.
    """
    claim_texts = _loads_claims(claim.claims_json)
    if not claim_texts and claim.entity_name:
        claim_texts = [claim.entity_name]
    evidence_items = list(claim.candidate_item.evidence_items if claim.candidate_item else [])
    return [_verify_one_claim(index, text, evidence_items, claim) for index, text in enumerate(claim_texts)]


def _verify_one_claim(
    claim_index: int,
    claim_text: str,
    evidence_items: list[EvidenceItem],
    claim: ExtractedClaim,
) -> ClaimVerificationDecision:
    support_items = [item for item in evidence_items if item.supports_claim == "support"]
    contradict_items = [item for item in evidence_items if item.supports_claim == "contradict"]
    neutral_items = [item for item in evidence_items if item.supports_claim in {"neutral", "unknown"}]

    claim_terms = _terms(claim_text, claim.entity_name)
    matched_support = [item for item in support_items if _evidence_matches_terms(item, claim_terms)] or support_items
    matched_contradict = [item for item in contradict_items if _evidence_matches_terms(item, claim_terms)] or contradict_items

    risk_flags = _collect_risk_flags(matched_contradict)
    if matched_contradict and (not matched_support or _max_confidence(matched_contradict) >= _max_confidence(matched_support)):
        supports_claim = "contradict"
        selected = matched_contradict
        confidence = max(80, _max_confidence(selected))
    elif matched_support:
        supports_claim = "support"
        selected = matched_support
        confidence = max(60, _max_confidence(selected))
    elif neutral_items:
        supports_claim = "neutral"
        selected = neutral_items[:3]
        confidence = min(55, max(35, _max_confidence(selected)))
    else:
        supports_claim = "unknown"
        selected = []
        confidence = 20
        risk_flags.append("no_claim_level_evidence")

    selected = sorted(selected, key=lambda item: (-item.evidence_confidence, -item.retrieval_score, item.id))[:5]
    return ClaimVerificationDecision(
        claim_index=claim_index,
        claim_text=claim_text,
        supports_claim=supports_claim,
        evidence_item_ids=[item.id for item in selected],
        confidence=_clamp(confidence),
        risk_flags=list(dict.fromkeys(risk_flags)),
        raw_response={
            "method": "deterministic_rules_v1",
            "support_count": len(matched_support),
            "contradict_count": len(matched_contradict),
            "neutral_count": len(neutral_items),
            "selected_evidence_ids": [item.id for item in selected],
        },
    )


def _loads_claims(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _terms(claim_text: str, entity_name: str | None) -> list[str]:
    values = [claim_text, entity_name or ""]
    terms: list[str] = []
    for value in values:
        text = value.lower()
        for token in ["mcp", "server", "install", "安装", "发布", "release", "released", "open", "weights", "gguf"]:
            if token in text:
                terms.append(token)
        if entity_name:
            terms.append(entity_name.lower())
    return list(dict.fromkeys(term for term in terms if term))


def _evidence_matches_terms(evidence: EvidenceItem, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = " ".join(
        part
        for part in [
            evidence.title or "",
            evidence.snippet or "",
            evidence.fetched_title or "",
            evidence.fetched_description or "",
            evidence.fetched_text_preview or "",
            evidence.source_domain or "",
        ]
        if part
    ).lower()
    return any(term in haystack for term in terms)


def _collect_risk_flags(evidence_items: list[EvidenceItem]) -> list[str]:
    flags: list[str] = []
    for item in evidence_items:
        try:
            data = json.loads(item.risk_flags or "[]")
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            flags.extend(str(flag) for flag in data)
    return flags


def _max_confidence(evidence_items: list[EvidenceItem]) -> int:
    if not evidence_items:
        return 0
    return max(_clamp(item.evidence_confidence) for item in evidence_items)


def _clamp(value: int | float | None) -> int:
    if value is None:
        return 0
    return max(0, min(int(value), 100))
