from __future__ import annotations

import json

from app.jobs.feedback_job import add_feedback, feedback_summary
from app.jobs.recommendation_export_job import run_recommendation_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import CanonicalEntity, UserFeedback
from tests.test_p2_entity_pipeline import _seed_verified_candidates
from app.jobs.entity_resolve_job import run_entity_resolve_job


def test_feedback_add_records_entity_action_and_summary(tmp_path):
    session_factory = _seed_verified_candidates(tmp_path / "feedback.db")
    run_entity_resolve_job(session_factory=session_factory, limit=10)

    with session_factory() as session:
        entity = session.query(CanonicalEntity).one()

    result = add_feedback(
        session_factory=session_factory,
        entity_id=entity.id,
        action="save",
        reason="useful MCP workflow",
    )

    assert result.inserted is True
    with session_factory() as session:
        row = session.query(UserFeedback).one()
        assert row.entity_id == entity.id
        assert row.action == "save"
        assert row.reason == "useful MCP workflow"

    summary = feedback_summary(session_factory=session_factory, entity_id=entity.id)
    assert summary["total"] == 1
    assert summary["actions"]["save"] == 1
    assert summary["positive"] == 1
    assert summary["negative"] == 0


def test_recommendation_export_includes_feedback_summary(tmp_path):
    session_factory = _seed_verified_candidates(tmp_path / "feedback_export.db")
    run_entity_resolve_job(session_factory=session_factory, limit=10)
    with session_factory() as session:
        entity = session.query(CanonicalEntity).one()
    add_feedback(session_factory=session_factory, entity_id=entity.id, action="like", reason=None)
    add_feedback(session_factory=session_factory, entity_id=entity.id, action="hide", reason="too much marketing")

    result = run_recommendation_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "out",
        limit=10,
    )

    payload = json.loads(result.jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["feedback"]["total"] == 2
    assert payload["feedback"]["positive"] == 1
    assert payload["feedback"]["negative"] == 1
    assert payload["feedback"]["actions"]["like"] == 1
    assert payload["feedback"]["actions"]["hide"] == 1
