from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.ai.claim_client import AIClaimExtractClient, ClaimExtractRequest
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import CandidateItemRepository, ExtractedClaimRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class ClaimExtractJobResult:
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_claim_extract_job(
    *,
    session_factory: sessionmaker[Session],
    client: AIClaimExtractClient,
    limit: int | None = 50,
    min_ai_score: int = 70,
) -> ClaimExtractJobResult:
    result = ClaimExtractJobResult()
    if not client.is_configured:
        raise RuntimeError("Claim extract API is not configured")

    with session_factory() as session:
        candidates = CandidateItemRepository(session).list_pending_for_claim_extract(
            limit=limit,
            min_ai_score=min_ai_score,
        )
        claim_repo = ExtractedClaimRepository(session)
        for candidate in candidates:
            result.processed += 1
            try:
                response = client.extract(_candidate_to_request(candidate))
                insert_result = claim_repo.insert_if_new(
                    candidate_item_id=candidate.id,
                    model=getattr(client, "model", None),
                    response=response,
                )
                if insert_result.inserted:
                    result.inserted += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                result.failed += 1
                error = f"candidate_item_id={candidate.id}: {exc}"
                result.errors.append(error)
                LOGGER.exception("Failed to extract claim for candidate item %s", candidate.id)
        session.commit()
    return result


def run_claim_extract_from_settings(
    *,
    settings: Settings,
    limit: int | None = 50,
    min_ai_score: int | None = None,
) -> ClaimExtractJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    client = AIClaimExtractClient.from_settings(settings)
    effective_min_ai_score = settings.claim_extract_min_ai_score if min_ai_score is None else min_ai_score
    return run_claim_extract_job(
        session_factory=session_factory,
        client=client,
        limit=limit,
        min_ai_score=effective_min_ai_score,
    )


def _candidate_to_request(candidate) -> ClaimExtractRequest:
    item = candidate.normalized_item
    ai_review = candidate.ai_review_item
    return ClaimExtractRequest(
        candidate_id=candidate.id,
        title=item.title,
        url=item.url,
        source_group=candidate.source_group,
        candidate_score=candidate.candidate_score,
        ai_score=ai_review.ai_score if ai_review else 0,
        ai_category=ai_review.category if ai_review else None,
        body_preview=_truncate(item.body_text or "", 1200),
        matched_keywords=_loads_json_list(candidate.matched_keywords),
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


def _truncate(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
