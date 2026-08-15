from __future__ import annotations

from app.config.limits import DEFAULT_DAILY_REPORT_LIMIT
from app.jobs.editorial_rank_job import EditorialProfile, load_daily_profile
from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, Source


def test_daily_profile_defaults_to_thirty_and_allows_explicit_overrides():
    profile = load_daily_profile()

    assert profile.total_max == DEFAULT_DAILY_REPORT_LIMIT
    assert profile.topic_caps["model"] == 16
    assert EditorialProfile.from_mapping({"total_max": 60}).total_max == 60
    assert EditorialProfile.from_mapping({"total_max": 0}).total_max == 0


def test_export_defaults_to_thirty_but_allows_explicit_overrides(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'export-cap.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        source = Source(
            id="cap-source",
            name="Cap source",
            transport="feed",
            url="https://example.test/feed",
            content_class="official_model_company",
        )
        session.add(source)
        for index in range(35):
            item = IntelItem(
                source=source,
                external_id=f"item-{index}",
                title=f"Item {index}",
                content_class="official_model_company",
                content_hash=f"{index:064x}",
                selection_score=index,
                status="selected",
            )
            item.ai_review = AIItemReview(
                content_class="official_model_company",
                keep=True,
                status="success",
            )
            session.add(item)
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
