from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.evidence.fetcher import EvidenceFetcher, EvidenceFetchResult
from app.evidence.special_verifiers import CompositeSpecialVerifier
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import EvidenceItem
from app.storage.repository import EvidenceItemRepository

LOGGER = logging.getLogger(__name__)


class SpecialVerifier(Protocol):
    def verify(self, evidence: EvidenceItem) -> EvidenceFetchResult | None: ...


@dataclass
class EvidenceFetchJobResult:
    processed: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_evidence_fetch_job(
    *,
    session_factory: sessionmaker[Session],
    fetcher: EvidenceFetcher,
    special_verifier: SpecialVerifier | None = None,
    limit: int | None = 50,
) -> EvidenceFetchJobResult:
    result = EvidenceFetchJobResult()
    with session_factory() as session:
        repo = EvidenceItemRepository(session)
        evidence_items = repo.list_pending_for_fetch(limit=limit)
        for evidence in evidence_items:
            result.processed += 1
            try:
                fetch_result = special_verifier.verify(evidence) if special_verifier else None
                if fetch_result is None:
                    fetch_result = fetcher.fetch(evidence)
                repo.update_fetch_result(
                    evidence_id=evidence.id,
                    http_status=fetch_result.http_status,
                    final_url=fetch_result.final_url,
                    url_validation_status=fetch_result.url_validation_status,
                    fetched_title=fetch_result.fetched_title,
                    fetched_description=fetch_result.fetched_description,
                    fetched_text_preview=fetch_result.fetched_text_preview,
                    raw_payload=fetch_result.raw_payload,
                    fetch_status="completed",
                    fetch_error=None,
                )
                result.updated += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"evidence_id={evidence.id}: {exc}")
                repo.update_fetch_result(
                    evidence_id=evidence.id,
                    http_status=None,
                    final_url=None,
                    url_validation_status="unknown",
                    fetched_title=None,
                    fetched_description=None,
                    fetched_text_preview=None,
                    raw_payload=None,
                    fetch_status="failed",
                    fetch_error=str(exc),
                )
                LOGGER.exception("Failed to fetch evidence item %s", evidence.id)
        session.commit()
    return result


def run_evidence_fetch_from_settings(
    *,
    settings: Settings,
    limit: int | None = 50,
) -> EvidenceFetchJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    fetcher = EvidenceFetcher(
        timeout_seconds=settings.evidence_fetch_timeout_seconds,
        max_bytes=settings.evidence_fetch_max_bytes,
        user_agent=settings.user_agent,
    )
    special_verifier = CompositeSpecialVerifier(
        user_agent=settings.user_agent,
        timeout_seconds=settings.evidence_fetch_timeout_seconds,
    )
    return run_evidence_fetch_job(
        session_factory=session_factory,
        fetcher=fetcher,
        special_verifier=special_verifier,
        limit=limit,
    )
