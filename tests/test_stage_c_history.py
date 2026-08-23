from __future__ import annotations

from types import SimpleNamespace

from app.jobs.event_cluster_job import _history_identity_index, _prepare_draft_novelty, _prepare_draft_substance
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import DailyEditionReportEntry
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _record(event_key: str, url: str) -> dict:
    return {
        "event_key": event_key,
        "title": event_key,
        "summary_cn": "历史摘要",
        "url": url,
        "source_refs": [{"source_url": url}],
    }


def test_history_window_reads_only_previous_three_calendar_dates_and_published_reports():
    session_factory = _db()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.replace_published_daily_report(
            edition_date="2026-08-21",
            records=[_record("event:d1", "https://history.example/d1")],
        )
        draft = repo.get_or_create_daily_edition("2026-08-20")
        session.add(
            DailyEditionReportEntry(
                edition_id=draft.id,
                event_key="event:d2-draft",
                display_order=1,
                title="未发布草稿",
            )
        )
        repo.replace_published_daily_report(
            edition_date="2026-08-19",
            records=[_record("event:d3", "https://history.example/d3")],
        )
        repo.replace_published_daily_report(
            edition_date="2026-08-18",
            records=[_record("event:d4", "https://history.example/d4")],
        )
        session.commit()

        rows = repo.list_prior_daily_report_entries(edition_date="2026-08-22", days=3)

        assert [row.event_key for row in rows] == ["event:d1", "event:d3"]


def test_novelty_guard_uses_history_identity_and_requires_material_change_for_updated():
    current_url = "https://vendor.example/releases/v2"
    history = [
        {
            "event_key": "event:previous",
            "source_refs": [{"source_url": current_url}],
        }
    ]
    admission = SimpleNamespace(
        item=SimpleNamespace(
            id=11,
            canonical_url=current_url,
            source_url=current_url,
            external_id="release-v2",
            title="Vendor release",
        )
    )
    common = {
        "draft_key": "draft-11",
        "aggregation_basis": [],
        "prior_event_key": None,
        "novelty_status": "new",
    }

    repeated = _prepare_draft_novelty(
        {**common, "material_changes": []},
        item_ids=[11],
        admissions={11: admission},
        history_by_key={"event:previous": history[0]},
        history_identity_index=_history_identity_index(history),
    )
    updated = _prepare_draft_novelty(
        {
            **common,
            "material_changes": [
                {
                    "change_type": "availability",
                    "claim": "开放范围发生变化。",
                    "supporting_item_ids": [11],
                }
            ],
        },
        item_ids=[11],
        admissions={11: admission},
        history_by_key={"event:previous": history[0]},
        history_identity_index=_history_identity_index(history),
    )

    assert repeated["novelty_status"] == "repeat"
    assert repeated["prior_event_key"] == "event:previous"
    assert updated["novelty_status"] == "updated"
    assert updated["material_changes"][0]["supporting_item_ids"] == [11]


def test_novelty_guard_rejects_history_keys_outside_the_loaded_window():
    admission = SimpleNamespace(
        item=SimpleNamespace(
            id=12,
            canonical_url="https://vendor.example/current",
            source_url="https://vendor.example/current",
            external_id="current",
            title="Current event",
        )
    )
    result = _prepare_draft_novelty(
        {
            "draft_key": "draft-12",
            "aggregation_basis": [],
            "novelty_status": "repeat",
            "prior_event_key": "event:older-than-three-days",
            "material_changes": [],
        },
        item_ids=[12],
        admissions={12: admission},
        history_by_key={},
        history_identity_index={},
    )

    assert result["novelty_status"] == "uncertain"
    assert result["prior_event_key"] is None
    assert "prior_event_outside_history_window" in result["risk_flags"]


def test_new_event_claiming_concrete_change_without_facts_needs_review():
    result = _prepare_draft_substance(
        {
            "substance_status": "concrete",
            "substantive_facts": [],
            "review_state": "candidate",
        },
        item_ids=[12],
        novelty_status="new",
    )

    assert result["substance_status"] == "uncertain"
    assert result["review_state"] == "needs_review"
    assert "concrete_event_without_substantive_facts" in result["risk_flags"]
    assert "uncertain_event_core" in result["risk_flags"]


def test_intent_only_event_is_rejected_even_when_attributed_details_are_present():
    result = _prepare_draft_substance(
        {
            "event_action": "other",
            "lifecycle_state": "announced",
            "substance_status": "intent_only",
            "substantive_facts": [
                {
                    "fact_type": "other",
                    "claim": "来源确认发言者提出了未来目标。",
                    "supporting_item_ids": [12],
                },
            ],
            "review_state": "needs_review",
        },
        item_ids=[12],
        novelty_status="new",
    )

    assert result["substance_status"] == "intent_only"
    assert result["review_state"] == "rejected"
    assert "intent_only_event" in result["risk_flags"]
    assert result["guard"]["policy"] == "three_state_substance_consistency_v2"


def test_uncertain_event_core_is_sent_to_review_without_event_type_rules():
    result = _prepare_draft_substance(
        {
            "event_action": "release",
            "lifecycle_state": "ga",
            "substance_status": "uncertain",
            "substantive_facts": [
                {
                    "fact_type": "availability",
                    "claim": "材料声称产品已经开放，但缺少可用性证据。",
                    "supporting_item_ids": [12],
                }
            ],
            "review_state": "candidate",
        },
        item_ids=[12],
        novelty_status="new",
    )

    assert result["substance_status"] == "uncertain"
    assert result["review_state"] == "needs_review"
    assert "uncertain_event_core" in result["risk_flags"]


def test_confirmed_repeat_without_material_change_is_rejected_even_if_model_requests_candidate():
    result = _prepare_draft_substance(
        {
            "substance_status": "concrete",
            "substantive_facts": [
                {
                    "fact_type": "product",
                    "claim": "材料直接支持该产品已经发布。",
                    "supporting_item_ids": [12],
                }
            ],
            "review_state": "candidate",
        },
        item_ids=[12],
        novelty_status="repeat",
    )

    assert result["substance_status"] == "concrete"
    assert result["review_state"] == "rejected"
    assert "confirmed_repeat_without_material_change" in result["risk_flags"]
    assert result["guard"]["requested_review_state"] == "candidate"
    assert result["guard"]["applied_review_state"] == "rejected"
