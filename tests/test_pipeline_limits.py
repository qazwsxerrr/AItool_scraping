from __future__ import annotations

from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.jobs.stage_d_job import StageDProfile, load_stage_d_profile
from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventStageDSnapshot


def test_stage_d_profile_defaults_to_thirty_and_caps_explicit_overrides():
    profile = load_stage_d_profile()

    assert profile.total_max == DEFAULT_DAILY_REPORT_LIMIT
    assert StageDProfile.from_mapping({"total_max": 60}).total_max == 30
    assert StageDProfile.from_mapping({"total_max": 0}).total_max == 0


def test_export_defaults_to_thirty_but_allows_explicit_overrides(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'export-cap.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        for index in range(35):
            event = IntelEvent(
                event_key=f"event-{index}",
                title=f"Item {index}",
                topic="model",
                summary_cn=f"Summary {index}",
                display_score=index,
            )
            session.add(event)
            session.flush()
            session.add(
                IntelEventStageDSnapshot(
                    snapshot_key="latest",
                    event_id=event.id,
                    display_order=index + 1,
                    display_score=index,
                    selected=True,
                    topic="model",
                    content_class="official_model_company",
                )
            )
        session.commit()

    default_result = run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "out",
    )
    override_result = run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "out-override",
        limit=100,
    )

    assert default_result.exported == DEFAULT_DAILY_REPORT_LIMIT
    assert override_result.exported == 35
    assert len((tmp_path / "out" / "intel_items.jsonl").read_text(encoding="utf-8").splitlines()) == DEFAULT_DAILY_REPORT_LIMIT
    assert len((tmp_path / "out-override" / "intel_items.jsonl").read_text(encoding="utf-8").splitlines()) == 35
