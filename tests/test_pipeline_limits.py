from __future__ import annotations

from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.jobs.export_job import _normalise_export_limit
from app.jobs.stage_d_job import StageDProfile, load_stage_d_profile


def test_stage_d_profile_defaults_to_thirty_and_caps_explicit_overrides():
    profile = load_stage_d_profile()

    assert profile.max_selected == DEFAULT_DAILY_REPORT_LIMIT
    assert StageDProfile.from_mapping({"max_selected": 60}).max_selected == 30
    assert StageDProfile.from_mapping({"max_selected": 0}).max_selected == 0


def test_export_limit_is_a_date_report_limit_not_a_stage_d_policy():
    assert _normalise_export_limit(None) == DEFAULT_DAILY_REPORT_LIMIT
    assert _normalise_export_limit(100) == 100
    assert _normalise_export_limit(0) == 0
