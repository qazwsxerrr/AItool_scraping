from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.pipeline.prefilter import evaluate_candidate
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import CandidateItemRepository, NormalizedItemRepository, SourceRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class PrefilterJobResult:
    processed: int = 0
    kept: int = 0
    dropped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_prefilter_job(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None = 100,
) -> PrefilterJobResult:
    result = PrefilterJobResult()
    with session_factory() as session:
        normalized_repo = NormalizedItemRepository(session)
        candidate_repo = CandidateItemRepository(session)
        source_repo = SourceRepository(session)
        items = normalized_repo.list_pending_for_prefilter(limit=limit)

        for item in items:
            result.processed += 1
            try:
                decision = evaluate_candidate(item)
                source_id = item.raw_item.source_id
                source_group, source_subtype = source_repo.get_source_metadata(source_id)
                insert_result = candidate_repo.insert_if_new(
                    normalized_item_id=item.id,
                    source_group=source_group,
                    source_subtype=source_subtype,
                    decision=decision,
                )
                if insert_result.inserted:
                    if decision.keep:
                        result.kept += 1
                    else:
                        result.dropped += 1
            except Exception as exc:
                result.failed += 1
                error = f"normalized_item_id={item.id}: {exc}"
                result.errors.append(error)
                LOGGER.exception("Failed to prefilter normalized item %s", item.id)

        session.commit()
    return result


def run_prefilter_from_settings(*, settings: Settings, limit: int | None = 100) -> PrefilterJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_prefilter_job(session_factory=session_factory, limit=limit)
