from __future__ import annotations

import json
from datetime import datetime, timezone

from app.ai import TriageResult
from app.config.settings import Settings
from app.domain.models import FetchItem, SourceSpec
from app.domain.policies import source_spec_from_config
from app.jobs.ai_review_job import run_ai_review_job
from app.jobs.editorial_rank_job import run_editorial_rank_job
from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs.fetch_job import IntelFetchResult, IntelSourceStats
from app.jobs import run_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelEvent, IntelItem
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'triage-integration.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _source(*, source_id: str = "official_triage_integration") -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name="Official triage integration",
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


class _FakeTriageProvider:
    model = "fake-triage"

    def __init__(self, *, paper: bool = False):
        self.paper = paper
        self.calls: list[int] = []

    def triage(self, envelope):
        self.calls.append(int(envelope.item_id))
        if self.paper:
            return TriageResult(
                item_id=envelope.item_id,
                keep=True,
                topic="paper",
                topics=["paper"],
                summary_cn="一篇待核实论文",
                keywords=["arXiv"],
                selection_score=88,
                scores={"relevance": 90, "total": 88},
                novelty="new",
                paper_support={"is_paper": True, "paper_url": envelope.url},
                risk_flags=[],
                reason="paper candidate",
                confidence=92,
                raw_response={"fixture": "arxiv"},
            )
        return TriageResult(
            item_id=envelope.item_id,
            keep=True,
            topic="model",
            topics=["model", "product"],
            summary_cn="模型更新摘要",
            keywords=["model", "release"],
            selection_score=93,
            scores={"relevance": 95, "total": 93},
            novelty="new",
            paper_support={"is_paper": False},
            risk_flags=["fixture:routed"],
            reason="model candidate",
            confidence=94,
            raw_response={"fixture": "model"},
        )


def _insert_item(session_factory, source: SourceSpec, *, url: str, title: str) -> None:
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source_spec_from_config(source))
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id=f"fixture:{title}",
                title=title,
                url=url,
                published_at=NOW,
                captured_at=NOW,
                summary=title,
                content_class=source.content_class,
            )
        )
        session.commit()


def test_triage_projection_drives_event_topic_without_network(tmp_path):
    session_factory = _db(tmp_path)
    source = _source()
    _insert_item(
        session_factory,
        source,
        url="https://official.example/model-release",
        title="Announcing a new model release",
    )
    provider = _FakeTriageProvider()

    review = run_ai_review_job(
        session_factory=session_factory,
        source_specs={source.id: source_spec_from_config(source)},
        ai_client=provider,
        limit=10,
        now=NOW,
        output_dir=tmp_path / "review",
    )
    events = run_event_cluster_job(session_factory=session_factory, limit=10)

    assert review.analyzed == 1
    assert provider.calls == [1]
    assert events.events == 1
    exported = json.loads((tmp_path / "review" / "ai_review_candidates.jsonl").read_text().splitlines()[0])
    assert exported["ai"]["topic"] == "model"
    assert exported["ai"]["paper_support"]["is_paper"] is False
    with session_factory() as session:
        item = session.get(IntelItem, 1)
        event = session.get(IntelEvent, 1)
        assert item is not None and item.ai_review is not None
        assert item.ai_review.topic == "model"
        assert json.loads(item.ai_review.topics_json) == ["model", "product"]
        assert json.loads(item.ai_review.keywords_json) == ["model", "release"]
        assert item.ai_review.selection_score == 93
        assert json.loads(item.ai_review.scores_json)["total"] == 93
        assert item.ai_review.novelty == "new"
        assert json.loads(item.ai_review.raw_response_json)["fixture"] == "model"
        assert event is not None
        assert event.topic == "model"
        assert json.loads(event.topics_json) == ["model", "product"]


def test_run_once_uses_injected_triage_provider_and_persists_event_topic(tmp_path, monkeypatch):
    db_path = tmp_path / "run-once.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    source = _source(source_id="run_once_triage_source")
    provider = _FakeTriageProvider()

    def fake_fetch(**kwargs):
        engine = create_engine_from_url(settings.database_url)
        init_db(engine)
        session_factory = create_session_factory(engine)
        _insert_item(
            session_factory,
            source,
            url="https://official.example/run-once-model",
            title="Announcing a new model release",
        )
        return IntelFetchResult(
            run_id=kwargs.get("run_id"),
            stats={source.id: IntelSourceStats(source_id=source.id, fetched=1, inserted=1, status="success")},
        )

    monkeypatch.setattr(run_job, "run_intel_fetch_from_settings", fake_fetch)
    result = run_job.run_intel_once_from_settings(
        settings=settings,
        source=source.id,
        limit=10,
        output_dir=str(tmp_path / "export"),
        ai_client=provider,
    )

    assert result.status == "completed"
    assert result.run_id is not None
    assert provider.calls == [1]
    assert result.event_cluster is not None and result.event_cluster.events == 1
    with create_session_factory(create_engine_from_url(settings.database_url))() as session:
        item = session.get(IntelItem, 1)
        event = session.get(IntelEvent, 1)
        assert item is not None and item.ai_review is not None
        assert item.ai_review.prompt_version == "intel_triage_v1"
        assert item.ai_review.topic == "model"
        assert item.ai_review.topics == ["model", "product"]
        assert item.ai_review.keywords == ["model", "release"]
        assert event is not None and event.topic == "model"


def test_arxiv_only_triage_is_rejected_and_blocked_by_editorial_paper_gate(tmp_path):
    session_factory = _db(tmp_path)
    source = _source(source_id="official_paper_integration")
    paper_url = "https://arxiv.org/abs/2608.12345"
    _insert_item(session_factory, source, url=paper_url, title="Research paper release")
    provider = _FakeTriageProvider(paper=True)

    review = run_ai_review_job(
        session_factory=session_factory,
        source_specs={source.id: source_spec_from_config(source)},
        ai_client=provider,
        limit=10,
        now=NOW,
        output_dir=tmp_path / "review",
    )
    run_event_cluster_job(session_factory=session_factory, limit=10)
    editorial = run_editorial_rank_job(session_factory=session_factory, limit=10)

    assert review.analyzed == 1
    with session_factory() as session:
        item = session.get(IntelItem, 1)
        event = session.get(IntelEvent, 1)
        assert item is not None and item.ai_review is not None
        assert item.status == "rejected"
        assert item.ai_review.keep is False
        assert "paper:arxiv_only" in json.loads(item.ai_review.risk_flags_json)
        assert event is not None and event.topic == "paper"
        assert "paper:arxiv_only" in json.loads(event.risk_flags_json)
    assert editorial.selected == 0
    assert editorial.rejected == 1


def test_run_once_paper_gate_blocks_arxiv_snapshot_without_network(tmp_path, monkeypatch):
    db_path = tmp_path / "run-once-paper.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    source = _source(source_id="run_once_paper_source")
    paper_url = "https://arxiv.org/abs/2608.54321"
    provider = _FakeTriageProvider(paper=True)

    def fake_fetch(**kwargs):
        engine = create_engine_from_url(settings.database_url)
        init_db(engine)
        session_factory = create_session_factory(engine)
        _insert_item(session_factory, source, url=paper_url, title="Research paper release")
        return IntelFetchResult(
            run_id=kwargs.get("run_id"),
            stats={source.id: IntelSourceStats(source_id=source.id, fetched=1, inserted=1, status="success")},
        )

    monkeypatch.setattr(run_job, "run_intel_fetch_from_settings", fake_fetch)
    result = run_job.run_intel_once_from_settings(
        settings=settings,
        source=source.id,
        limit=10,
        output_dir=str(tmp_path / "export"),
        ai_client=provider,
    )

    assert result.status == "completed"
    assert result.event_cluster is not None and result.event_cluster.snapshots == 1
    assert result.editorial_rank is not None and result.editorial_rank.selected == 0
    with create_session_factory(create_engine_from_url(settings.database_url))() as session:
        item = session.get(IntelItem, 1)
        event = session.get(IntelEvent, 1)
        assert item is not None and item.status == "rejected"
        assert event is not None and event.topic == "paper"
        assert "paper:arxiv_only" in json.loads(event.risk_flags_json)
