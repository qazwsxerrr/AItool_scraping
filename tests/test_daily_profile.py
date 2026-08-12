from __future__ import annotations

from pathlib import Path

import pytest

from app.config.daily_profile import DailyProfile, load_daily_profile


def test_default_daily_profile_loads_with_aggregate_github_cap():
    profile = load_daily_profile()

    assert profile.target_events == 20
    assert profile.minimum_publishable_events == 14
    assert sum(section.target for section in profile.sections.values()) == 20
    assert profile.github_aggregate_cap == 3
    assert profile.source_group_caps["github_trending"] == 3
    assert profile.source_group_caps["github_release"] == 3
    assert profile.source_group_caps["github_search"] == 3


def test_profile_rejects_unknown_fields_and_bad_section_total():
    raw = {
        "target_events": 20,
        "sections": {
            "model_product": {"target": 5},
            "industry_infrastructure": {"target": 4},
            "research": {"target": 3},
            "open_source_tool": {"target": 4},
            "practice_opinion": {"target": 3},
        },
        "unexpected": True,
    }

    with pytest.raises(ValueError, match="extra_forbidden|section target total"):
        DailyProfile.model_validate(raw)


def test_profile_rejects_per_subgroup_github_cap_as_aggregate():
    raw = {
        "target_events": 20,
        "sections": {
            "model_product": {"target": 5},
            "industry_infrastructure": {"target": 4},
            "research": {"target": 3},
            "open_source_tool": {"target": 4},
            "practice_opinion": {"target": 4},
        },
        "source_group_caps": {
            "github_trending": 3,
            "github_release": 3,
            "github_search": 3,
            "reddit_search": 0,
        },
        "aggregate_caps": {"github": 9},
    }

    profile = DailyProfile.model_validate(raw)
    assert profile.github_aggregate_cap == 9


def test_profile_loader_rejects_non_mapping(tmp_path: Path):
    path = tmp_path / "daily_profile.yaml"
    path.write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a mapping"):
        load_daily_profile(path)
