from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.ai.skills.intel_triage import AnalysisResult, ScreenResult
from app.domain.models import FetchItem, SourceSpec
from app.jobs.stage_a_screen_job import run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import run_stage_b_analysis_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem
from app.storage.repository import IntelRepository


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'stage-ab.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _source() -> SourceSpec:
    return SourceSpec(
        id="stage_ab_test",
        name="Stage A/B test",
        transport="feed",
        url="https://official.example/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_subtype="fixed_news",
        source_role="official",
        content_class="official_model_company",
    )


def _daily_build(session_factory, source: SourceSpec, titles: list[str]) -> int:
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        _, build = repo.start_daily_build(edition_date="2026-08-19", reference_time=now)
        for index, title in enumerate(titles, start=1):
            repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=f"item:{index}",
                    title=title,
                    url=f"https://official.example/{index}",
                    summary=title,
                    published_at=now,
                    captured_at=now,
                ),
                run_id=build.id,
            )
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id)


class _AI:
    model = "test-model"

    def __init__(
        self,
        *,
        reject_titles: dict[str, int] | None = None,
        fail_screen_titles: set[str] | None = None,
        score_by_title: dict[str, int] | None = None,
        paper_titles: set[str] | None = None,
        fail_analysis_titles: set[str] | None = None,
        track_concurrency: bool = False,
    ):
        self.reject_titles = reject_titles or {}
        self.fail_screen_titles = fail_screen_titles or set()
        self.score_by_title = score_by_title or {}
        self.paper_titles = paper_titles or set()
        self.fail_analysis_titles = fail_analysis_titles or set()
        self.screen_calls: list[int] = []
        self.analysis_calls: list[int] = []
        self.track_concurrency = track_concurrency
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def _enter(self) -> None:
        if not self.track_concurrency:
            return
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        time.sleep(0.01)

    def _exit(self) -> None:
        if self.track_concurrency:
            with self._lock:
                self._active -= 1

    def screen(self, envelope):
        self._enter()
        try:
            self.screen_calls.append(envelope.item_id)
            if envelope.title in self.fail_screen_titles:
                raise RuntimeError("screen timeout")
            if envelope.title in self.reject_titles:
                return ScreenResult(
                    item_id=envelope.item_id,
                    decision="reject",
                    reason_code="not_relevant",
                    reason="screen decision",
                    confidence=self.reject_titles[envelope.title],
                    risk_flags=["screen_fixture"],
                    raw_response={"fixture": "screen"},
                )
            return ScreenResult(
                item_id=envelope.item_id,
                decision="pass",
                reason_code="relevant",
                reason="screen pass",
                confidence=90,
                risk_flags=[],
                raw_response={"fixture": "screen"},
            )
        finally:
            self._exit()

    def analyze(self, envelope):
        self._enter()
        try:
            self.analysis_calls.append(envelope.item_id)
            if envelope.title in self.fail_analysis_titles:
                raise RuntimeError("analysis timeout")
            score = self.score_by_title.get(envelope.title, 80)
            return AnalysisResult(
                item_id=envelope.item_id,
                topic="paper" if envelope.title in self.paper_titles else "product",
                topics=["paper"] if envelope.title in self.paper_titles else ["product"],
                summary_cn="中文阶段 B 摘要",
                keywords=["model", "release"],
                entities=[],
                selection_score=score,
                score_components={"total": score},
                paper_support={
                    "is_paper": envelope.title in self.paper_titles,
                    "paper_url": envelope.url if envelope.title in self.paper_titles else None,
                    "arxiv_only": envelope.title in self.paper_titles,
                },
                risk_flags=[],
                reason="analysis fixture",
                confidence=90,
                raw_response={"fixture": "analysis"},
            )
        finally:
            self._exit()


def test_daily_stage_a_b_keeps_low_score_and_paper_for_stage_d(tmp_path):
    session_factory = _db(tmp_path)
    source = _source()
    run_id = _daily_build(session_factory, source, ["high reject", "low score", "paper", "candidate"])
    ai = _AI(
        reject_titles={"high reject": 90},
        score_by_title={"low score": 59, "paper": 99, "candidate": 60},
        paper_titles={"paper"},
    )

    stage_a = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
    )
    stage_b = run_stage_b_analysis_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
    )

    assert stage_a.screened_out == 1
    assert stage_b.analyzed == 3
    assert stage_b.analysis_filtered == 0
    assert stage_b.candidate == 3
    with session_factory() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert [row.status for row in rows] == ["screened_out", "candidate", "candidate", "candidate"]
        assert "analysis:low_signal" in rows[1].ai_review.risk_flags
        assert "paper:arxiv_only" in rows[2].ai_review.risk_flags


def test_daily_stage_a_b_isolates_provider_failures(tmp_path):
    session_factory = _db(tmp_path)
    source = _source()
    run_id = _daily_build(session_factory, source, ["screen fail", "analysis fail", "good"])
    ai = _AI(fail_screen_titles={"screen fail"}, fail_analysis_titles={"analysis fail"})

    stage_a = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
    )
    stage_b = run_stage_b_analysis_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
    )

    assert stage_a.screen_failed == 1
    assert stage_b.analysis_failed == 1
    assert stage_b.candidate == 1
    with session_factory() as session:
        assert session.scalars(select(IntelItem.status).order_by(IntelItem.id)).all() == [
            "screen_failed",
            "analysis_failed",
            "candidate",
        ]


def test_daily_stage_a_b_bounds_provider_concurrency(tmp_path):
    session_factory = _db(tmp_path)
    source = _source()
    run_id = _daily_build(session_factory, source, [f"item-{index}" for index in range(8)])
    ai = _AI(track_concurrency=True)

    run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
        concurrency=20,
    )
    run_stage_b_analysis_job(
        session_factory=session_factory,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
        concurrency=20,
    )

    assert ai.max_active <= 4
