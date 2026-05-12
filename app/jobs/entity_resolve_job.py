from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import EntityRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class EntityResolveJobResult:
    processed: int = 0
    entities_created: int = 0
    mentions_created: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_entity_resolve_job(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None = 100,
) -> EntityResolveJobResult:
    result = EntityResolveJobResult()
    with session_factory() as session:
        repo = EntityRepository(session)
        rows = repo.list_unmentioned_verifications(limit=limit)
        for verification in rows:
            result.processed += 1
            try:
                _, entity_created, mention_created = repo.resolve_verification(verification)
                if entity_created:
                    result.entities_created += 1
                if mention_created:
                    result.mentions_created += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"verification_id={verification.id}: {exc}")
                LOGGER.exception("Failed to resolve entity for verification item %s", verification.id)
        session.commit()
    return result


def run_entity_resolve_from_settings(
    *,
    settings: Settings,
    limit: int | None = 100,
) -> EntityResolveJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_entity_resolve_job(session_factory=session_factory, limit=limit)
