from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.pipeline.claim_verification import verify_claims_for_extracted_claim
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import ClaimVerificationRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class ClaimVerifyJobResult:
    processed_claims: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_claim_verify_job(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None = 100,
) -> ClaimVerifyJobResult:
    result = ClaimVerifyJobResult()
    with session_factory() as session:
        repo = ClaimVerificationRepository(session)
        claims = repo.list_pending_claims(limit=limit)
        for claim in claims:
            result.processed_claims += 1
            try:
                decisions = verify_claims_for_extracted_claim(claim)
                for decision in decisions:
                    insert_result = repo.insert_if_new(
                        candidate_item_id=claim.candidate_item_id,
                        extracted_claim_id=claim.id,
                        claim_index=decision.claim_index,
                        claim_text=decision.claim_text,
                        supports_claim=decision.supports_claim,
                        evidence_item_ids=decision.evidence_item_ids,
                        confidence=decision.confidence,
                        risk_flags=decision.risk_flags,
                        raw_response=decision.raw_response,
                    )
                    if insert_result.inserted:
                        result.inserted += 1
                    else:
                        result.skipped += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"extracted_claim_id={claim.id}: {exc}")
                LOGGER.exception("Failed to verify claim item %s", claim.id)
        session.commit()
    return result


def run_claim_verify_from_settings(
    *,
    settings: Settings,
    limit: int | None = 100,
) -> ClaimVerifyJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_claim_verify_job(session_factory=session_factory, limit=limit)
