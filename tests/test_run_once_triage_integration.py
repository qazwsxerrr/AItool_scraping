from __future__ import annotations

from datetime import datetime, timezone

from app.ai.skills.intel_triage import AnalysisResult, ScreenResult
from app.config.settings import Settings
from app.domain.models import FetchItem, SourceSpec
from app.domain.policies import source_spec_from_config
from app.jobs import run_job
from app.jobs.ai_review_job import run_ai_review_job
from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs.fetch_job import IntelFetchResult, IntelSourceStats
from app.jobs.stage_d_job import run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelEventItem, IntelEventStageDSnapshot, IntelItem
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'staged-integration.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _source(source_id: str = "official_stage_integration") -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name="Official staged integration",
        transport="feed",
        url="https://official.example/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_subtype="fixed_news",
        source_role="official",
        content_class="official_model_company",
        tier="p1",
        fetch_interval=1,
    )


class _FakeIntelProvider:
    model = "fake-staged-provider"

    def __init__(self, *, paper: bool = False):
        self.paper = paper
        self.screen_calls: list[int] = []
        self.analysis_calls: list[int] = []

    def screen(self, envelope):
        self.screen_calls.append(int(envelope.item_id))
        return ScreenResult(
            item_id=envelope.item_id,
            decision="pass",
            reason_code="valuable_signal",
            reason="fixture pass",
            confidence=95,
            raw_response={"fixture": "screen"},
        )

    def analyze(self, envelope):
        self.analysis_calls.append(int(envelope.item_id))
        if self.paper:
            return AnalysisResult(
                item_id=envelope.item_id,
                topic="paper",
                topics=["paper"],
                summary_cn="一篇待核实论文",
                keywords=["arXiv"],
                selection_score=88,
                paper_support={"is_paper": True, "paper_url": envelope.url},
                risk_flags=[],
                reason="paper candidate",
                confidence=92,
                raw_response={"fixture": "paper"},
            )
        return AnalysisResult(
            item_id=envelope.item_id,
            topic="model",
            topics=["model", "product"],
            summary_cn="模型更新摘要",
            keywords=["model", "release"],
            selection_score=93,
            paper_support={"is_paper": False},
            risk_flags=["fixture:routed"],
            reason="model candidate",
            confidence=94,
            raw_response={"fixture": "analysis"},
        )


def _insert_item(session_factory, source: SourceSpec, *, url: str, title: str) -> None:
    captured_at = datetime.now(timezone.utc)
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source_spec_from_config(source))
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id=f"fixture:{title}",
                title=title,
                url=url,
                published_at=captured_at,
                captured_at=captured_at,
                summary=title,
                content_class=source.content_class,
            )
        )
        session.commit()


def test_stage_a_b_projection_drives_current_event(tmp_path):
    session_factory = _db(tmp_path)
    source = _source()
    _insert_item(session_factory, source, url="https://official.example/model-release", title="Announcing a new model release")
    provider = _FakeIntelProvider()

    review = run_ai_review_job(
        session_factory=session_factory,
        source_specs={source.id: source_spec_from_config(source)},
        ai_client=provider,
        limit=10,
        now=NOW,
        output_dir=tmp_path / "review",
    )
    events = run_event_cluster_job(session_factory=session_factory, item_ids=review.candidate_ids)
    editorial = run_stage_d_job(session_factory=session_factory, event_ids=events.event_ids)

    assert review.candidate == 1
    assert provider.screen_calls == [1]
    assert provider.analysis_calls == [1]
    assert events.event_ids == [1]
    assert editorial.selected == 1
    with session_factory() as session:
        item = session.get(IntelItem, 1)
        event = session.get(IntelEvent, 1)
        assert item is not None and item.status == "candidate"
        assert item.ai_screen is not None and item.ai_screen.decision == "pass"
        assert item.ai_review is not None and item.ai_review.topic == "model"
        assert event is not None and event.topic == "model"
        assert session.query(IntelEventStageDSnapshot).count() == 1


def test_repeat_item_attaches_to_history_without_current_new_event(tmp_path):
    session_factory = _db(tmp_path)
    source = _source("repeat_source")
    _insert_item(session_factory, source, url="https://official.example/same", title="Same model release")
    provider = _FakeIntelProvider()
    first = run_ai_review_job(session_factory=session_factory, source_specs={source.id: source_spec_from_config(source)}, ai_client=provider, limit=10, output_dir=tmp_path / "review-1")
    first_events = run_event_cluster_job(session_factory=session_factory, item_ids=first.candidate_ids, now=NOW)
    assert first_events.event_ids == [1]

    _insert_item(session_factory, source, url="https://official.example/same", title="Same model release (follow-up)")
    second = run_ai_review_job(session_factory=session_factory, source_specs={source.id: source_spec_from_config(source)}, ai_client=provider, limit=10, force=True, output_dir=tmp_path / "review-2")
    second_events = run_event_cluster_job(session_factory=session_factory, item_ids=second.candidate_ids, now=NOW)
    assert second_events.event_ids == []
    with session_factory() as session:
        assert session.query(IntelEvent).count() == 1
        assert session.query(IntelEventItem).count() == 2
