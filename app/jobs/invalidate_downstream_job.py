from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import ClaimVerificationItem, RecommendationCard, VerificationItem

InvalidateFromStage = Literal["evidence", "claim-verification", "verification"]


@dataclass(frozen=True)
class InvalidateDownstreamJobResult:
    from_stage: str
    claim_verifications: int = 0
    verification_items: int = 0
    recommendation_cards: int = 0


def run_invalidate_downstream_job(
    *,
    session_factory: sessionmaker[Session],
    from_stage: InvalidateFromStage,
) -> InvalidateDownstreamJobResult:
    if from_stage not in {"evidence", "claim-verification", "verification"}:
        raise ValueError(f"unsupported from_stage: {from_stage}")

    with session_factory() as session:
        now = datetime.now(timezone.utc)
        claim_count = 0
        verification_count = 0
        card_count = 0

        if from_stage == "evidence":
            claim_count = _mark_stale(
                session,
                ClaimVerificationItem,
                {"stale": True, "updated_at": now},
            )

        if from_stage in {"evidence", "claim-verification"}:
            verification_count = _mark_stale(
                session,
                VerificationItem,
                {"stale": True, "updated_at": now},
            )

        card_count = _mark_stale(
            session,
            RecommendationCard,
            {"stale": True, "updated_at": now},
        )
        session.commit()

    return InvalidateDownstreamJobResult(
        from_stage=from_stage,
        claim_verifications=claim_count,
        verification_items=verification_count,
        recommendation_cards=card_count,
    )


def run_invalidate_downstream_from_settings(
    *,
    settings: Settings,
    from_stage: InvalidateFromStage,
) -> InvalidateDownstreamJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_invalidate_downstream_job(session_factory=session_factory, from_stage=from_stage)


def _mark_stale(session: Session, model, values: dict) -> int:
    return (
        session.query(model)
        .filter(model.stale.is_(False))
        .update(values, synchronize_session=False)
    )
