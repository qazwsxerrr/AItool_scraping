from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.ai.review_client import AIReviewClient, AIReviewRequest
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import AIReviewItemRepository, CandidateItemRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class AIReviewJobResult:
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_ai_review_job(
    *,
    session_factory: sessionmaker[Session],
    client: AIReviewClient,
    limit: int | None = 20,
) -> AIReviewJobResult:
    result = AIReviewJobResult()
    if not client.is_configured:
        raise RuntimeError("AI review API is not configured")

    with session_factory() as session:
        candidate_repo = CandidateItemRepository(session)
        ai_review_repo = AIReviewItemRepository(session)
        candidates = candidate_repo.list_pending_for_ai_review(limit=limit)

        for candidate in candidates:
            result.processed += 1
            try:
                response = client.review(_candidate_to_request(candidate))
                insert_result = ai_review_repo.insert_if_new(
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
                LOGGER.exception("Failed to AI-review candidate item %s", candidate.id)

        session.commit()
    return result


def run_ai_review_from_settings(*, settings: Settings, limit: int | None = 20) -> AIReviewJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    client = AIReviewClient.from_settings(settings)
    return run_ai_review_job(session_factory=session_factory, client=client, limit=limit)


def _candidate_to_request(candidate) -> AIReviewRequest:
    item = candidate.normalized_item
    return AIReviewRequest(
        candidate_id=candidate.id,
        title=item.title,
        url=item.url,
        source_group=candidate.source_group,
        candidate_score=candidate.candidate_score,
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
