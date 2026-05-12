from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Any

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.jobs.ai_review_job import run_ai_review_from_settings
from app.jobs.ai_verify_job import run_ai_verify_from_settings
from app.jobs.claim_extract_job import run_claim_extract_from_settings
from app.jobs.claim_verify_job import run_claim_verify_from_settings
from app.jobs.entity_resolve_job import run_entity_resolve_from_settings
from app.jobs.evidence_classify_job import run_evidence_classify_from_settings
from app.jobs.evidence_fetch_job import run_evidence_fetch_from_settings
from app.jobs.evidence_search_job import run_evidence_search_from_settings
from app.jobs.fetch_job import run_fetch_from_registry
from app.jobs.normalize_job import run_normalize_from_settings
from app.jobs.prefilter_job import run_prefilter_from_settings
from app.jobs.recommendation_export_job import run_recommendation_export_from_settings
from app.jobs.recommendation_write_job import run_recommendation_write_from_settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import PipelineRunRepository


@dataclass(frozen=True)
class PipelineRunJobResult:
    run_id: int
    status: str
    stats: dict[str, Any]
    error: str | None = None


def run_daily_job(
    *,
    session_factory: sessionmaker[Session],
    steps: list[tuple[str, Callable[[], Any]]],
    run_type: str = "daily",
) -> PipelineRunJobResult:
    stats: dict[str, Any] = {}
    error: str | None = None
    with session_factory() as session:
        repo = PipelineRunRepository(session)
        run = repo.start(run_type=run_type)
        session.commit()
        run_id = run.id

    status = "completed"
    try:
        for name, step in steps:
            stats[name] = _result_to_dict(step())
    except Exception as exc:
        status = "failed"
        error = str(exc)

    with session_factory() as session:
        PipelineRunRepository(session).finish(run_id, status=status, stats=stats, error=error)
        session.commit()
    return PipelineRunJobResult(run_id=run_id, status=status, stats=stats, error=error)


def run_daily_from_settings(*, settings: Settings) -> PipelineRunJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    steps = [
        ("fetch", lambda: run_fetch_from_registry(settings=settings)),
        ("normalize", lambda: run_normalize_from_settings(settings=settings, limit=500)),
        ("prefilter", lambda: run_prefilter_from_settings(settings=settings, limit=500)),
        ("ai_review", lambda: run_ai_review_from_settings(settings=settings, limit=80)),
        ("claim_extract", lambda: run_claim_extract_from_settings(settings=settings, limit=80)),
        ("evidence_search", lambda: run_evidence_search_from_settings(settings=settings, limit=50)),
        ("evidence_fetch", lambda: run_evidence_fetch_from_settings(settings=settings, limit=80)),
        ("evidence_classify", lambda: run_evidence_classify_from_settings(settings=settings, limit=120)),
        ("claim_verify", lambda: run_claim_verify_from_settings(settings=settings, limit=120)),
        ("ai_verify", lambda: run_ai_verify_from_settings(settings=settings, limit=50)),
        ("entity_resolve", lambda: run_entity_resolve_from_settings(settings=settings, limit=100)),
        ("recommendation_write", lambda: run_recommendation_write_from_settings(settings=settings, limit=100)),
        ("recommendation_export", lambda: run_recommendation_export_from_settings(settings=settings, limit=20)),
    ]
    return run_daily_job(session_factory=session_factory, steps=steps, run_type="daily")


def _result_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"result": str(value)}
