from __future__ import annotations

import json
import threading
import time

from sqlalchemy import select

from app.ai.skills.intel_triage import AnalysisResult, ScreenResult
from app.domain.models import FetchItem, SourceSpec
from app.jobs.ai_review_job import run_ai_review_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem, IntelRun, IntelRunItem
from app.storage.repository import IntelRepository


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'ai-review.db'}")
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
        fetch_interval=1,
    )


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
        if not self.track_concurrency:
            return
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
            if envelope.title in self.paper_titles:
                return AnalysisResult(
                    item_id=envelope.item_id,
                    topic="paper",
                    topics=["paper"],
                    summary_cn="论文摘要",
                    keywords=["paper"],
                    entities=[],
                    selection_score=score,
                    score_components={"total": score},
                    paper_support={
                        "is_paper": True,
                        "paper_url": envelope.url,
                        "arxiv_only": True,
                    },
                    risk_flags=[],
                    reason="paper fixture",
                    confidence=90,
                    raw_response={"fixture": "analysis"},
                )
            return AnalysisResult(
                item_id=envelope.item_id,
                topic="product",
                topics=["product"],
                summary_cn="中文阶段 B 摘要",
                keywords=["model", "release"],
                entities=[],
                selection_score=score,
                score_components={"relevance": score, "total": score},
                paper_support={"is_paper": False},
                risk_flags=[],
                reason="analysis fixture",
                confidence=90,
                raw_response={"fixture": "analysis"},
            )
        finally:
            self._exit()


def _insert_items(session_factory, source: SourceSpec, titles: list[str], *, run_id: int | None = None):
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        for index, title in enumerate(titles, 1):
            repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=f"item:{index}:{title}",
                    title=title,
                    url=f"https://official.example/{index}/{title.replace(' ', '-')}",
                    summary=title,
                ),
                run_id=run_id,
            )
        session.commit()


def test_stage_a_stage_b_guards_and_exports(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    titles = ["high reject", "low reject", "low score", "paper", "candidate"]
    _insert_items(sf, source, titles)
    ai = _AI(
        reject_titles={"high reject": 90, "low reject": 20},
        score_by_title={"low score": 59, "paper": 99, "candidate": 60},
        paper_titles={"paper"},
    )

    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ai,
        output_dir=tmp_path / "out",
    )

    assert result.processed == 5
    assert result.screened == 5
    assert result.screened_out == 1
    assert result.analyzed == 4
    assert result.analysis_filtered == 2
    assert result.candidate == 2
    assert len(result.candidate_ids) == 2
    assert result.exported == 2
    assert result.audit_exported == 5
    assert len(ai.screen_calls) == 5
    assert len(ai.analysis_calls) == 4

    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert [row.status for row in rows] == [
            "screened_out",
            "candidate",
            "analysis_filtered",
            "analysis_filtered",
            "candidate",
        ]
        assert rows[0].ai_review is None
        assert rows[1].ai_review.selection_score == 80
        assert rows[2].ai_review.selection_score == 59
        assert rows[3].ai_review.paper_support["arxiv_only"] is True
        assert rows[4].ai_review.selection_score == 60

    candidate_records = [
        json.loads(line)
        for line in (tmp_path / "out" / "ai_review_candidates.jsonl").read_text().splitlines()
    ]
    assert {record["status"] for record in candidate_records} == {"candidate"}
    assert all("keep" not in record and "novelty" not in record for record in candidate_records)
    assert {record["screen"]["decision"] for record in candidate_records} == {"pass", "uncertain"}
    assert candidate_records[0]["analysis"]["summary_cn"] == "中文阶段 B 摘要"


def test_low_confidence_reject_is_uncertain_and_reaches_stage_b(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    _insert_items(sf, source, ["low reject"])
    ai = _AI(reject_titles={"low reject": 84})

    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ai,
        output_dir=tmp_path / "out",
    )

    assert result.screened_out == 0
    assert result.analyzed == 1
    assert ai.analysis_calls == [1]
    with sf() as session:
        item = session.scalar(select(IntelItem))
        assert item.status == "candidate"
        assert item.ai_screen.decision == "uncertain"
        assert "screen:low_confidence_reject" in item.ai_screen.risk_flags


def test_screen_failure_isolated_and_stage_b_not_called(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    _insert_items(sf, source, ["screen fail", "good"])
    ai = _AI(fail_screen_titles={"screen fail"})

    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ai,
        output_dir=tmp_path / "out",
    )

    assert result.screen_failed == 1
    assert result.analyzed == 1
    assert ai.analysis_calls == [2]
    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert rows[0].status == "screen_failed"
        assert rows[0].ai_screen.status == "screen_failed"
        assert rows[0].ai_review is None
        assert rows[1].status == "candidate"


def test_analysis_failure_isolated_and_auditable(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    _insert_items(sf, source, ["analysis fail", "good"])
    ai = _AI(fail_analysis_titles={"analysis fail"})

    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ai,
        output_dir=tmp_path / "out",
    )

    assert result.analysis_failed == 1
    assert result.candidate == 1
    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert rows[0].status == "analysis_failed"
        assert rows[0].ai_review.status == "analysis_failed"
        assert "analysis timeout" in (rows[0].ai_review.error_message or "")
        assert "after 6 attempts" in (rows[0].ai_review.error_message or "")
        assert rows[1].status == "candidate"


def test_run_scope_and_force_are_run_local(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        run = repo.start_run(run_type="run_once")
        session.commit()
        run_id = run.id
    _insert_items(sf, source, ["scoped"], run_id=run_id)
    _insert_items(sf, source, ["historical"])

    ai = _AI()
    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
        output_dir=tmp_path / "out",
    )

    assert result.candidate_ids == [1]
    assert ai.screen_calls == [1]
    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert rows[0].status == "candidate"
        assert rows[1].status == "new"
        assert session.scalar(select(IntelRunItem).where(IntelRunItem.run_id == run_id)).item_id == 1


def test_explicit_ai_limit_marks_run_and_output_partial(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        run = repo.start_run(run_type="run_once")
        session.commit()
        run_id = run.id
    _insert_items(sf, source, ["first", "second"], run_id=run_id)
    ai = _AI()
    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ai,
        run_id=run_id,
        limit=100,
        output_dir=tmp_path / "out",
    )

    assert result.partial is True
    assert result.partial_reason == "ai_limit:100"
    with sf() as session:
        run = session.get(IntelRun, run_id)
        assert run.partial is True
        assert run.partial_reason == "ai_limit:100"
    record = json.loads((tmp_path / "out" / "ai_review_candidates.jsonl").read_text().splitlines()[0])
    assert record["run_partial"] is True
    assert record["run_counts"]["candidate"] == 2


def test_provider_concurrency_is_bounded_to_four(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    _insert_items(sf, source, [f"item-{index}" for index in range(8)])
    ai = _AI(track_concurrency=True)

    run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ai,
        output_dir=tmp_path / "out",
        concurrency=20,
    )

    assert ai.max_active <= 4
