from __future__ import annotations

from datetime import date, datetime, timezone

from app.domain.models import FetchItem
from app.jobs.stage_b_analysis_job import materialize_stage_b_admission
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, DailyEdition, IntelItem, Source
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _seed_analysis(session_factory, rows: list[tuple[str, int, str]]) -> tuple[int, dict[str, int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        session.add(Source(
            id="b-source", name="B source", transport="feed", url="https://b.example/feed.xml",
            source_group="official_blog", source_role="official", primary_eligible=True,
            content_class="official_model_company",
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


def test_b_admission_applies_score_gate_and_keeps_duplicate_support_as_reserve():
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

    result = materialize_stage_b_admission(session_factory=session_factory, run_id=run_id, min_score=60, reserve_limit=20)

    assert result is not None
    assert result.target == 100
    assert set(result.active_ids) == {ids["high leader"], ids["threshold pass"]}
    assert result.reserve_ids == (ids["duplicate support"],)
    assert result.filtered_count == 1
    with session_factory() as session:
        rows = IntelRepository(session).list_candidate_admissions(run_id)
        decisions = {row.item_id: row.decision for row in rows}
        assert decisions[ids["threshold fail"]] == "filtered"
        assert session.get(IntelItem, ids["threshold fail"]).status == "analysis_filtered"


def test_b_admission_uses_calibrated_dynamic_target_after_fourteen_editions():
    session_factory = _db()
    with session_factory() as session:
        for day in range(1, 15):
            session.add(DailyEdition(
                edition_date=date(2026, 8, day), status="published", published_at=NOW,
                candidate_count=40, selected_count=30,
            ))
        session.commit()
    rows = [(f"event {index}", 80, f"https://b.example/{index}") for index in range(70)]
    run_id, _ = _seed_analysis(session_factory, rows)

    result = materialize_stage_b_admission(session_factory=session_factory, run_id=run_id, min_score=60, reserve_limit=20)

    assert result is not None
    # 30 * P75(40 / 30) is below the lower bound, so the active workbench
    # uses the planned minimum rather than silently expanding to every item.
    assert result.target == 60
    assert len(result.active_ids) == 60
    assert len(result.reserve_ids) == 10
