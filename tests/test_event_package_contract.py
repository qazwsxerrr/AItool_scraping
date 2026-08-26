from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from app.ai.skills.event_package import (
    CANDIDATE_EVENT_PACKAGE_FIELDS,
    build_candidate_event_package,
)
from app.ai.skills.stage_c_agent.prompts import SAVE_DRAFTS_SCHEMA


def test_stage_c_draft_schema_does_not_author_keyword_or_entity_fields():
    properties = SAVE_DRAFTS_SCHEMA["properties"]["drafts"]["items"]["properties"]
    required = SAVE_DRAFTS_SCHEMA["properties"]["drafts"]["items"]["required"]

    assert "keywords" not in properties
    assert "entities" not in properties
    assert "keywords" not in required
    assert "entities" not in required
    assert "split_reason" in properties
    assert "split_reason" in required
    assert "published_at" not in properties


def test_candidate_event_package_contains_only_contract_fields():
    event = SimpleNamespace(
        id=7,
        title="标题",
        summary_cn="摘要",
        topic="model_release",
        source_group="official_blog",
        source_groups_json='["official_blog"]',
        last_seen_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
        first_seen_at=None,
        review_state="candidate",
        novelty_status="new",
        risk_flags_json='["intent_only_event", "confirmed_repeat_without_material_change", "needs_review"]',
        resolution_raw_json=json.dumps(
            {
                "draft_metadata": {
                    "event_family_key": "acme_release",
                    "facts": [{"claim": "已发布", "supporting_item_ids": [1]}],
                    "publishability": "candidate",
                    "history_status": "new",
                    "split_reason": None,
                    "caveats": ["厂商自测数据"],
                }
            }
        ),
    )

    package = build_candidate_event_package(event)

    assert list(package) == list(CANDIDATE_EVENT_PACKAGE_FIELDS)
    assert package["eligibility_blockers"] == ["confirmed_repeat_without_material_change"]
    assert "needs_review" not in package["editorial_caveats"]
    assert "intent_only_event" in package["editorial_caveats"]
    assert "厂商自测数据" in package["editorial_caveats"]
    assert "display_score" not in package
    assert "review_state" not in package
    assert "novelty_status" not in package
    assert "keywords" not in package
    assert "entities" not in package
    assert "search_evidence" not in package
    assert "split_reason" not in package
    assert "published_at" not in package
