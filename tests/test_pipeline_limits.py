from __future__ import annotations

from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.jobs.export_job import _normalise_export_limit
from app.jobs.stage_d_job import StageDProfile, load_stage_d_profile


def test_stage_d_profile_defaults_to_thirty_and_caps_explicit_overrides():
    profile = load_stage_d_profile()

    assert profile.total_max == DEFAULT_DAILY_REPORT_LIMIT
    assert StageDProfile.from_mapping({"total_max": 60}).total_max == 30
    assert StageDProfile.from_mapping({"total_max": 0}).total_max == 0


def test_export_limit_is_a_date_report_limit_not_a_legacy_snapshot_limit():
    assert _normalise_export_limit(None) == DEFAULT_DAILY_REPORT_LIMIT
    assert _normalise_export_limit(100) == 100
    assert _normalise_export_limit(0) == 0
