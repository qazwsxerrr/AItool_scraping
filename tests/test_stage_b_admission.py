from __future__ import annotations

import json
from datetime import datetime, timezone

from app.domain.models import FetchItem
from app.jobs.stage_b_analysis_job import _admission_sort_key, materialize_stage_b_admission
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, Source
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def test_b_admission_tie_break_uses_content_class_without_tier_or_role():
    official = IntelItem(
        id=1, build_id=1, source_id="official", title="Official", content_hash="a",
        content_class="official_model_company", captured_at=NOW,
    )
    community = IntelItem(
        id=2, build_id=1, source_id="community", title="Community", content_hash="b",
        content_class="community_social", captured_at=NOW,
    )

    assert _admission_sort_key((official, 80)) < _admission_sort_key((community, 80))


def _seed_analysis(session_factory, rows: list[tuple[str, int, str]]) -> tuple[int, dict[str, int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        session.add(Source(
            id="b-source", name="B source", transport="feed", url="https://b.example/feed.xml",
            source_group="official_blog", content_class="official_model_company",
        ))
        session.flush()
        _, run = repo.start_daily_build(edition_date="2026-08-20", reference_time=NOW)
        stage = repo.ensure_stage(run.id, "analyze")
        ids: dict[str, int] = {}
        for index, (title, score, url) in enumerate(rows, start=1):
            inserted = repo.insert_item(
                FetchItem(
                    source_id="b-source", external_id=f"b-{index}", title=title, summary=title,
                    url=url, content_class="official_model_company", published_at=NOW, captured_at=NOW,
                ),
                run_id=run.id,
            )
            assert inserted.item_id is not None
            item_id = int(inserted.item_id)
            ids[title] = item_id
            session.add(AIItemReview(
                item_id=item_id, content_class="official_model_company", topic="model_release",
                topics_json='["model_release"]', keywords_json='["model"]', entities_json='[]',
                summary_cn=title, b1_priority=score, status="success",
            ))
            task = repo.ensure_stage_task(stage, subject_type="item", subject_id=item_id, item_id=item_id)
            repo.complete_stage_task(task, result={"b1_priority": score})
        repo.finish_stage(stage, status="succeeded")
        session.commit()
        return int(run.id), ids


def test_b_admission_filters_low_score_but_keeps_duplicate_url_items_active():
    session_factory = _db()
    run_id, ids = _seed_analysis(
        session_factory,
        [
            ("high leader", 91, "https://b.example/model"),
            ("duplicate support", 85, "https://b.example/model"),
            ("threshold pass", 60, "https://b.example/threshold"),
            ("threshold fail", 59, "https://b.example/fail"),
        ],
    )

    result = materialize_stage_b_admission(session_factory=session_factory, run_id=run_id, reserve_limit=20)

    assert result is not None
    assert result.target == 3
    assert set(result.active_ids) == {
        ids["high leader"],
        ids["duplicate support"],
        ids["threshold pass"],
    }
    assert result.reserve_ids == ()
    assert result.filtered_count == 1
    with session_factory() as session:
        rows = IntelRepository(session).list_candidate_admissions(run_id)
        decisions = {row.item_id: row.decision for row in rows}
        assert decisions[ids["threshold fail"]] == "filtered"
        assert session.get(IntelItem, ids["threshold fail"]).status == "analysis_filtered"


def test_b_admission_keeps_all_structurally_valid_items_without_a_quota():
    session_factory = _db()
    rows = [(f"event {index}", 80, f"https://b.example/{index}") for index in range(70)]
    run_id, _ = _seed_analysis(session_factory, rows)

    result = materialize_stage_b_admission(session_factory=session_factory, run_id=run_id, reserve_limit=20)

    assert result is not None
    assert result.target == 70
    assert len(result.active_ids) == 70
    assert result.reserve_ids == ()
    assert result.filtered_count == 0


def test_b_admission_filters_low_ai_subject_relevance():
    session_factory = _db()
    run_id, ids = _seed_analysis(
        session_factory,
        [
            ("generic cloud update", 95, "https://b.example/cloud"),
            ("boundary ai update", 60, "https://b.example/boundary"),
            ("direct model release", 80, "https://b.example/model"),
        ],
    )
    with session_factory() as session:
        below_gate = session.get(AIItemReview, ids["generic cloud update"])
        at_gate = session.get(AIItemReview, ids["boundary ai update"])
        assert below_gate is not None
        assert at_gate is not None
        below_gate.score_components_json = json.dumps(
            {
                "audience_relevance": 59,
                "material_change": 100,
                "impact_scope": 100,
                "independent_news_value": 100,
                "specificity": 100,
            }
        )
        at_gate.score_components_json = json.dumps(
            {
                "audience_relevance": 60,
                "material_change": 60,
                "impact_scope": 60,
                "independent_news_value": 60,
                "specificity": 60,
            }
        )
        session.commit()

    result = materialize_stage_b_admission(session_factory=session_factory, run_id=run_id)

    assert result is not None
    assert ids["generic cloud update"] not in result.active_ids
    assert ids["boundary ai update"] in result.active_ids
    with session_factory() as session:
        rows = IntelRepository(session).list_candidate_admissions(run_id)
        row = next(item for item in rows if item.item_id == ids["generic cloud update"])
        assert row.decision == "filtered"
        assert row.reason_code == "below_ai_relevance_threshold"


def test_b_admission_does_not_treat_previous_filtered_status_as_structural():
    session_factory = _db()
    run_id, ids = _seed_analysis(
        session_factory,
        [("previously filtered", 80, "https://b.example/old-gate")],
    )
    with session_factory() as session:
        item = session.get(IntelItem, ids["previously filtered"])
        assert item is not None
        item.status = "analysis_filtered"
        item.selection_reason = "below_score_threshold"
        session.commit()

    result = materialize_stage_b_admission(session_factory=session_factory, run_id=run_id)

    assert result is not None
    assert result.active_ids == (ids["previously filtered"],)
    assert result.filtered_count == 0
    with session_factory() as session:
        item = session.get(IntelItem, ids["previously filtered"])
        assert item is not None
        assert item.status == "candidate"
