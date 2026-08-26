from __future__ import annotations

from types import SimpleNamespace

from app.jobs.event_cluster_job import _history_identity_index, _prepare_draft_history, _prepare_draft_publishability
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


def test_history_guard_uses_identity_and_requires_facts_for_meaningful_update():
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
        "event_family_key": "vendor_release",
        "prior_event_key": None,
        "history_status": "new",
    }

    repeated = _prepare_draft_history(
        {**common, "facts": []},
        item_ids=[11],
        admissions={11: admission},
        history_by_key={"event:previous": history[0]},
        history_identity_index=_history_identity_index(history),
    )
    updated = _prepare_draft_history(
        {
            **common,
            "history_status": "meaningful_update",
            "facts": [
                {
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

    assert repeated["history_status"] == "repeat"
    assert repeated["novelty_status"] == "repeat"
    assert repeated["prior_event_key"] == "event:previous"
    assert updated["history_status"] == "meaningful_update"
    assert updated["novelty_status"] == "updated"


def test_history_guard_rejects_history_keys_outside_the_loaded_window():
    admission = SimpleNamespace(
        item=SimpleNamespace(
            id=12,
            canonical_url="https://vendor.example/current",
            source_url="https://vendor.example/current",
            external_id="current",
            title="Current event",
        )
    )
    result = _prepare_draft_history(
        {
            "draft_key": "draft-12",
            "event_family_key": "vendor_current",
            "history_status": "repeat",
            "prior_event_key": "event:older-than-three-days",
            "facts": [],
        },
        item_ids=[12],
        admissions={12: admission},
        history_by_key={},
        history_identity_index={},
    )

    assert result["history_status"] == "uncertain"
    assert result["novelty_status"] == "uncertain"
    assert result["prior_event_key"] is None
    assert "prior_event_outside_history_window" in result["risk_flags"]


def test_candidate_without_facts_becomes_needs_review():
    result = _prepare_draft_publishability(
        {
            "publishability": "candidate",
            "facts": [],
        },
        item_ids=[12],
        novelty_status="new",
    )

    assert result["publishability"] == "needs_review"
    assert result["review_state"] == "needs_review"
    assert "candidate_without_facts" in result["risk_flags"]


def test_needs_review_keeps_fact_boundary_and_audit_flag():
    result = _prepare_draft_publishability(
        {
            "publishability": "needs_review",
            "facts": [
                {
                    "claim": "材料声称产品已经开放，但缺少可用性证据。",
                    "supporting_item_ids": [12],
                }
            ],
        },
        item_ids=[12],
        novelty_status="new",
    )

    assert result["publishability"] == "needs_review"
    assert result["review_state"] == "needs_review"
    assert "uncertain_event_core" in result["risk_flags"]


def test_confirmed_repeat_without_meaningful_update_is_rejected_even_if_model_requests_candidate():
    result = _prepare_draft_publishability(
        {
            "publishability": "candidate",
            "facts": [
                {
                    "claim": "材料直接支持该产品已经发布。",
                    "supporting_item_ids": [12],
                }
            ],
        },
        item_ids=[12],
        novelty_status="repeat",
    )

    assert result["publishability"] == "rejected"
    assert result["review_state"] == "rejected"
    assert "confirmed_repeat_without_material_change" in result["risk_flags"]
    assert result["guard"]["requested_publishability"] == "candidate"
    assert result["guard"]["applied_review_state"] == "rejected"
