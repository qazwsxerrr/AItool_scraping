from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.evidence.classifier import classify_evidence
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import EvidenceItemRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class EvidenceClassifyJobResult:
    processed: int = 0
    updated: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_evidence_classify_job(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None = 100,
    force: bool = False,
    classification_version: str = "rules_v1",
) -> EvidenceClassifyJobResult:
    result = EvidenceClassifyJobResult()
    with session_factory() as session:
        repo = EvidenceItemRepository(session)
        evidence_items = repo.list_pending_for_classify(
            limit=limit,
            force=force,
            classification_version=classification_version,
        )
        for evidence in evidence_items:
            result.processed += 1
            try:
                classification = classify_evidence(evidence)
                repo.update_classification(
                    evidence_id=evidence.id,
                    supports_claim=classification.supports_claim,
                    evidence_confidence=classification.evidence_confidence,
                    risk_flags=classification.risk_flags,
                    quality_flags=classification.quality_flags,
                    classification_version=classification_version,
                )
                result.updated += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"evidence_id={evidence.id}: {exc}")
                repo.mark_classification_failed(evidence.id, str(exc))
                LOGGER.exception("Failed to classify evidence item %s", evidence.id)
        session.commit()
    return result


def run_evidence_classify_from_settings(
    *,
    settings: Settings,
    limit: int | None = 100,
    force: bool = False,
    classification_version: str = "rules_v1",
) -> EvidenceClassifyJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_evidence_classify_job(
        session_factory=session_factory,
        limit=limit,
        force=force,
        classification_version=classification_version,
    )
