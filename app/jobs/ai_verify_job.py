from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.ai.verify_client import AIVerifyClient, AIVerifyRequest
from app.config.settings import Settings
from app.pipeline.freshness import calculate_freshness_score
from app.pipeline.source_quality import source_quality_for_source
from app.pipeline.verification import finalize_verification
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import VerificationItemRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class AIVerifyJobResult:
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_ai_verify_job(
    *,
    session_factory: sessionmaker[Session],
    client: AIVerifyClient,
    limit: int | None = 30,
    min_score: int = 75,
    min_credibility: int = 60,
    max_spam_risk: int = 40,
) -> AIVerifyJobResult:
    result = AIVerifyJobResult()
    if not client.is_configured:
        raise RuntimeError("AI verify API is not configured")

    with session_factory() as session:
        repo = VerificationItemRepository(session)
        candidates = repo.list_pending_for_ai_verify(limit=limit)
        for candidate in candidates:
            result.processed += 1
            try:
                request = _candidate_to_request(candidate)
                response = client.verify(request)
                final = finalize_verification(
                    response,
                    evidence_count=len(candidate.evidence_items),
                    min_score=min_score,
                    min_credibility=min_credibility,
                    max_spam_risk=max_spam_risk,
                )
                freshness_score = calculate_freshness_score(candidate)
                insert_result = repo.insert_if_new(
                    candidate_item_id=candidate.id,
                    model=getattr(client, "model", None),
                    verification=final,
                    freshness_score=freshness_score,
                )
                if insert_result.inserted:
                    result.inserted += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                result.failed += 1
                error = f"candidate_item_id={candidate.id}: {exc}"
                result.errors.append(error)
                LOGGER.exception("Failed to AI-verify candidate item %s", candidate.id)
        session.commit()
    return result


def run_ai_verify_from_settings(
    *,
    settings: Settings,
    limit: int | None = 30,
) -> AIVerifyJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    client = AIVerifyClient.from_settings(settings)
    return run_ai_verify_job(
        session_factory=session_factory,
        client=client,
        limit=limit,
        min_score=settings.final_review_min_score,
        min_credibility=settings.final_review_min_credibility,
        max_spam_risk=settings.final_review_max_spam_risk,
    )


def _candidate_to_request(candidate) -> AIVerifyRequest:
    item = candidate.normalized_item
    raw_item = item.raw_item
    ai_review = candidate.ai_review_item
    claim = candidate.extracted_claim
    return AIVerifyRequest(
        candidate={
            "candidate_id": candidate.id,
            "title": item.title,
            "url": item.url,
            "source_id": raw_item.source_id,
            "source_group": candidate.source_group,
            "source_subtype": candidate.source_subtype,
            "candidate_score": candidate.candidate_score,
            "matched_keywords": _loads_json_list(candidate.matched_keywords),
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "body_preview": _truncate(item.body_text or "", 1200),
        },
        ai_review={
            "ai_keep": ai_review.ai_keep if ai_review else None,
            "ai_score": ai_review.ai_score if ai_review else None,
            "category": ai_review.category if ai_review else None,
            "reason": ai_review.reason if ai_review else None,
            "summary_cn": ai_review.summary_cn if ai_review else None,
        },
        extracted_claim=(
            {
                "entity_name": claim.entity_name,
                "entity_type": claim.entity_type,
                "official_url": claim.official_url,
                "github_url": claim.github_url,
                "huggingface_url": claim.huggingface_url,
                "producthunt_url": claim.producthunt_url,
                "main_claims": _loads_json_list(claim.claims_json),
                "release_signal": claim.release_signal,
                "actionable_signal": claim.actionable_signal,
                "confidence": claim.confidence,
            }
            if claim
            else None
        ),
        evidence_items=[
            {
                "evidence_type": evidence.evidence_type,
                "url": evidence.url,
                "title": evidence.title,
                "snippet": evidence.snippet,
                "source_domain": evidence.source_domain,
                "supports_claim": evidence.supports_claim,
                "retrieval_score": evidence.retrieval_score,
                "evidence_confidence": evidence.evidence_confidence,
                "url_validation_status": evidence.url_validation_status,
                "http_status": evidence.http_status,
                "final_url": evidence.final_url,
                "fetched_title": evidence.fetched_title,
                "fetched_text_preview": _truncate(evidence.fetched_text_preview or "", 800),
                "risk_flags": _loads_json_list(evidence.risk_flags),
                "quality_flags": _loads_json_list(evidence.quality_flags),
                "query": evidence.query,
            }
            for evidence in sorted(
                candidate.evidence_items,
                key=lambda row: (-row.evidence_confidence, -row.retrieval_score, row.id),
            )
        ],
        source_quality=source_quality_for_source(raw_item.source, fallback_group=candidate.source_group),
        claim_verifications=[
            {
                "claim_index": row.claim_index,
                "claim_text": row.claim_text,
                "supports_claim": row.supports_claim,
                "evidence_item_ids": _loads_json_int_list(row.evidence_item_ids_json),
                "confidence": row.confidence,
                "risk_flags": _loads_json_list(row.risk_flags),
            }
            for row in sorted(
                getattr(claim, "claim_verification_items", None) or candidate.claim_verification_items,
                key=lambda item: (item.claim_index, item.id),
            )
        ],
    )


def _loads_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _loads_json_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    result: list[int] = []
    for item in data:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _truncate(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
