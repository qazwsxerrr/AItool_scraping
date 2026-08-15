from __future__ import annotations

import threading
import time

from sqlalchemy import select

from app.ai.skills.intel_triage import AnalysisResult, ScreenResult, preflight_intel_triage_schemas
from app.ai.skills.intel_triage.prompts import INTEL_ANALYSIS_JSON_SCHEMA
from app.domain.models import FetchItem, SourceSpec
from app.jobs.stage_a_screen_job import run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import run_stage_b_analysis_job
from app.jobs.ai_review_job import run_ai_review_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem, IntelRun, IntelRunStageTask
from app.storage.repository import IntelRepository


def _factory(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'resumable-ab.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _source() -> SourceSpec:
    return SourceSpec(
        id="resumable-ab",
        name="Resumable A/B",
        transport="feed",
        url="https://example.test/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_subtype="fixed_news",
        source_role="official",
        content_class="official_model_company",
    )


def _run_with_items(session_factory, titles: list[str]) -> tuple[SourceSpec, int]:
    source = _source()
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=source)
        run = repo.start_run(run_type="resumable_test")
        for index, title in enumerate(titles, 1):
            repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=f"item:{index}",
                    title=title,
                    url=f"https://example.test/{index}",
                    summary=title,
                ),
                run_id=run.id,
            )
        session.commit()
        return source, int(run.id)


class _Provider:
    model = "resumable-test-model"

    def __init__(self, *, reject: set[str] | None = None, fail_screen: set[str] | None = None, fail_analysis: set[str] | None = None):
        self.reject = reject or set()
        self.fail_screen = fail_screen or set()
        self.fail_analysis = fail_analysis or set()
        self.screen_calls: list[int] = []
        self.analysis_calls: list[int] = []

    def screen(self, envelope):
        self.screen_calls.append(int(envelope.item_id))
        if envelope.title in self.fail_screen:
            raise RuntimeError("screen timeout")
        if envelope.title in self.reject:
            return ScreenResult(
                item_id=envelope.item_id,
                decision="reject",
                reason_code="noise",
                reason="not relevant",
                confidence=99,
                risk_flags=[],
                raw_response={"fixture": "reject"},
            )
        return ScreenResult(
            item_id=envelope.item_id,
            decision="pass",
            reason_code="relevant",
            reason="relevant",
            confidence=95,
            risk_flags=[],
            raw_response={"fixture": "pass"},
        )

    def analyze(self, envelope):
        self.analysis_calls.append(int(envelope.item_id))
        if envelope.title in self.fail_analysis:
            self.fail_analysis.remove(envelope.title)
            raise RuntimeError("analysis timeout")
        return AnalysisResult(
            item_id=envelope.item_id,
            topic="product",
            topics=["product"],
            summary_cn="中文摘要",
            keywords=["release"],
            entities=[],
            selection_score=88,
            score_components={"total": 88},
            paper_support={"is_paper": False},
            risk_flags=[],
            reason="candidate",
            confidence=90,
            raw_response={"fixture": "analysis"},
        )


def test_reject_stops_stage_b(tmp_path):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["reject", "pass"])
    provider = _Provider(reject={"reject"})

    a = run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    b = run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)

    assert a.screened_out == 1
    assert b.candidate == 1
    assert provider.analysis_calls == [2]
    with sf() as session:
        statuses = session.scalars(select(IntelItem.status).order_by(IntelItem.id)).all()
        assert statuses == ["screened_out", "candidate"]


def test_stage_b_retry_does_not_call_stage_a(tmp_path):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["one"])
    provider = _Provider(fail_analysis={"one"})

    run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    first_b = run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    second_b = run_stage_b_analysis_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        retry_failed=True,
    )

    assert first_b.analysis_failed == 1
    assert second_b.candidate == 1
    assert provider.screen_calls == [1]
    assert provider.analysis_calls == [1, 1]


def test_successful_tasks_skip_until_force_is_scoped_to_stage(tmp_path):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["one"])
    provider = _Provider()

    run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    assert provider.screen_calls == [1]
    assert provider.analysis_calls == [1]

    run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id, force=True)
    assert provider.screen_calls == [1]
    assert provider.analysis_calls == [1, 1]


def test_force_can_be_scoped_to_one_item(tmp_path):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["one", "two"])
    provider = _Provider()
    run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)

    run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        force=True,
        item_ids=[1],
    )
    run_stage_b_analysis_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        force=True,
        item_ids=[1],
    )
    assert provider.screen_calls == [1, 2, 1]
    assert provider.analysis_calls == [1, 2, 1]


def test_item_failure_isolated_from_other_items(tmp_path):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["fails", "works"])
    provider = _Provider(fail_screen={"fails"})

    a = run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    b = run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)

    assert a.screen_failed == 1
    assert a.screened == 1
    assert b.candidate == 1
    assert provider.analysis_calls == [2]


def test_provider_retryable_and_blocked_failures_are_distinguished(tmp_path):
    class ProviderError(RuntimeError):
        def __init__(self, status_code):
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    class Errors(_Provider):
        def screen(self, envelope):
            self.screen_calls.append(int(envelope.item_id))
            raise ProviderError(429 if envelope.title == "retry" else 400)

    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["retry", "blocked"])
    provider = Errors()
    run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    with sf() as session:
        stage = IntelRepository(session).get_stage(run_id, "screen")
        tasks = IntelRepository(session).list_stage_tasks(stage, subject_type="item")
        assert [task.status for task in tasks] == ["retry_waiting", "blocked"]


def test_provider_concurrency_is_bounded_and_facade_forwards_limit(tmp_path):
    class Concurrent(_Provider):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self._active = 0
            self.max_active = 0

        def _enter(self):
            with self._lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)

        def _exit(self):
            with self._lock:
                self._active -= 1

        def screen(self, envelope):
            self._enter()
            try:
                time.sleep(0.03)
                return super().screen(envelope)
            finally:
                self._exit()

        def analyze(self, envelope):
            self._enter()
            try:
                time.sleep(0.03)
                return super().analyze(envelope)
            finally:
                self._exit()

    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, [f"item-{index}" for index in range(8)])
    provider = Concurrent()
    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        concurrency=2,
    )

    assert result.candidate == 8
    assert provider.max_active > 1
    assert provider.max_active <= 2


def test_strict_schema_preflight_rejects_missing_aliases():
    entities = INTEL_ANALYSIS_JSON_SCHEMA["properties"]["entities"]["items"]
    original = list(entities["required"])
    entities["required"] = ["name", "type"]
    try:
        try:
            preflight_intel_triage_schemas()
        except ValueError as exc:
            assert "aliases" in str(exc)
        else:
            raise AssertionError("missing aliases must fail strict schema preflight")
    finally:
        entities["required"] = original
    assert preflight_intel_triage_schemas() is True
