from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import UserFeedbackRepository


@dataclass(frozen=True)
class FeedbackAddResult:
    inserted: bool
    feedback_id: int


def add_feedback(
    *,
    session_factory: sessionmaker[Session],
    entity_id: int | None = None,
    candidate_item_id: int | None = None,
    action: str,
    reason: str | None = None,
) -> FeedbackAddResult:
    with session_factory() as session:
        item = UserFeedbackRepository(session).add(
            entity_id=entity_id,
            candidate_item_id=candidate_item_id,
            action=action,
            reason=reason,
        )
        session.commit()
        return FeedbackAddResult(inserted=True, feedback_id=item.id)


def feedback_summary(
    *,
    session_factory: sessionmaker[Session],
    entity_id: int | None = None,
    candidate_item_id: int | None = None,
) -> dict:
    with session_factory() as session:
        return UserFeedbackRepository(session).summary(entity_id=entity_id, candidate_item_id=candidate_item_id)


def add_feedback_from_settings(
    *,
    settings: Settings,
    entity_id: int | None = None,
    candidate_item_id: int | None = None,
    action: str,
    reason: str | None = None,
) -> FeedbackAddResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return add_feedback(
        session_factory=session_factory,
        entity_id=entity_id,
        candidate_item_id=candidate_item_id,
        action=action,
        reason=reason,
    )


def feedback_summary_from_settings(
    *,
    settings: Settings,
    entity_id: int | None = None,
    candidate_item_id: int | None = None,
) -> dict:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return feedback_summary(
        session_factory=session_factory,
        entity_id=entity_id,
        candidate_item_id=candidate_item_id,
    )
