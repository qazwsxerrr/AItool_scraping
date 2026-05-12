from __future__ import annotations

import json
from dataclasses import dataclass

from app.storage.models import EvidenceItem


@dataclass(frozen=True)
class EvidenceClassification:
    supports_claim: str
    evidence_confidence: int
    risk_flags: list[str]
    quality_flags: list[str]


def classify_evidence(evidence: EvidenceItem) -> EvidenceClassification:
    risk_flags: list[str] = []
    quality_flags: list[str] = []
    supports_claim = "unknown"
    confidence = max(evidence.evidence_confidence, 30)

    payload = _loads_dict(evidence.raw_payload)
    provider = str(payload.get("provider") or "")

    if evidence.url_validation_status in {"unreachable", "invalid"} or evidence.http_status in {404, 410}:
        supports_claim = "contradict"
        confidence = max(confidence, 90)
        risk_flags.append("broken_primary_link")
        if evidence.url_validation_status == "invalid":
            risk_flags.append("hallucinated_url")
        return EvidenceClassification(supports_claim, confidence, risk_flags, quality_flags)

    if evidence.url_validation_status == "forbidden":
        supports_claim = "unknown"
        confidence = max(confidence, 35)
        risk_flags.append("forbidden")

    if evidence.evidence_type == "github_repo" or provider == "github":
        if payload.get("repo_exists") is False:
            return EvidenceClassification("contradict", 90, ["broken_github_repo"], [])
        supports_claim = "support"
        quality_flags.extend(_string_list(payload.get("quality_flags")))
        risk_flags.extend(_string_list(payload.get("risk_flags")))
        if payload.get("readme_exists"):
            quality_flags.append("readme_exists")
        if payload.get("license"):
            quality_flags.append("has_license")
        confidence = max(confidence, 80 if "has_license" in quality_flags else 70)
        return EvidenceClassification(supports_claim, confidence, risk_flags, quality_flags)

    if evidence.evidence_type == "huggingface_model" or provider == "huggingface":
        if payload.get("model_exists") is False:
            return EvidenceClassification("contradict", 90, ["broken_huggingface_model"], [])
        supports_claim = "support"
        quality_flags.extend(_string_list(payload.get("quality_flags")))
        risk_flags.extend(_string_list(payload.get("risk_flags")))
        if payload.get("card_exists"):
            quality_flags.append("model_card")
        if payload.get("has_weights"):
            quality_flags.append("has_weights")
        confidence = max(confidence, 80 if "has_weights" in quality_flags else 65)
        return EvidenceClassification(supports_claim, confidence, risk_flags, quality_flags)

    combined_text = " ".join(
        part
        for part in [
            evidence.fetched_title or evidence.title or "",
            evidence.fetched_description or "",
            evidence.fetched_text_preview or evidence.snippet or "",
        ]
        if part
    ).lower()
    claim = evidence.candidate_item.extracted_claim if evidence.candidate_item else None
    entity_name = (claim.entity_name or "").lower() if claim else ""
    if evidence.url_validation_status in {"reachable", "redirected"} and entity_name and entity_name in combined_text:
        supports_claim = "support"
        confidence = max(confidence, 70)
        quality_flags.append("entity_name_match")
    elif evidence.url_validation_status in {"reachable", "redirected"}:
        supports_claim = "neutral"
        confidence = max(confidence, 45)

    return EvidenceClassification(supports_claim, confidence, risk_flags, quality_flags)


def _loads_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
