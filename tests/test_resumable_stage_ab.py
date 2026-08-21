from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.ai.skills.intel_triage import AnalysisResult, ScreenResult, preflight_intel_triage_schemas
from app.ai.skills.intel_triage.prompts import INTEL_ANALYSIS_JSON_SCHEMA
from app.domain.models import FetchItem, SourceSpec
from app.jobs.stage_a_screen_job import run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import run_stage_b_analysis_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemScreen, IntelItem, IntelRun, IntelRunStageTask
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
        reference_time = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)
        _, run = repo.start_daily_build(edition_date="2026-08-16", reference_time=reference_time)
        for index, title in enumerate(titles, 1):
            repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=f"item:{index}",
                    title=title,
                    url=f"https://example.test/{index}",
                    summary=title,
                    published_at=reference_time,
                    captured_at=reference_time,
                ),
                run_id=run.id,
            )
        repo.freeze_run_scope(run.id)
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
                reason_code="spam",
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
            topic="product_application",
            topics=["product_application"],
            summary_cn="中文摘要",
            keywords=["release"],
            entities=[],
            b1_priority=88,
            score_components={
                "audience_relevance": 88,
                "material_change": 88,
                "impact_scope": 88,
                "independent_news_value": 88,
                "specificity": 88,
            },
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

    assert first_b.analysis_failed == 0
    assert first_b.candidate == 1
    # Reusing a successful B task still rematerializes the run-level
    # active/reserve projection, so the C workbench remains observable.
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


def test_force_stage_b_replaces_terminal_stage_a_ineligible_projection(tmp_path):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["old", "reject", "keep"])
    provider = _Provider()

    run_stage_a_screen_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)
    run_stage_b_analysis_job(session_factory=sf, source_specs={source.id: source}, ai_client=provider, run_id=run_id)

    # Reprocess the same run: one former candidate falls outside the frozen
    # window and another becomes a Stage-A reject.  Their old Stage-B tasks
    # must be terminally skipped, not reset to permanently pending.
    with sf() as session:
        old = session.get(IntelItem, 1)
        assert old is not None
        old.published_at = datetime(2026, 8, 13, 10, 59, 59, tzinfo=timezone.utc)
        session.commit()
    provider.reject.add("reject")

    screened = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        force=True,
        limit=None,
    )
    analyzed = run_stage_b_analysis_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        force=True,
        limit=None,
    )

    assert screened.time_filter_counts == {"too_old": 1}
    assert screened.screened_out == 1
    assert analyzed.candidate == 1
    assert analyzed.skipped == 2
    with sf() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "analyze")
        assert stage is not None and stage.status == "succeeded"
        tasks = {
            int(task.item_id): task
            for task in repo.list_stage_tasks(stage, subject_type="item", include_expired=True)
        }
        assert {item_id: task.status for item_id, task in tasks.items()} == {
            1: "skipped",
            2: "skipped",
            3: "succeeded",
        }
        # Current projection changes, but immutable provider attempts remain
        # available for audit.
        assert all(repo.list_stage_attempts(task) for task in tasks.values())


def test_limits_only_mark_partial_when_eligible_work_is_deferred(tmp_path):
    sf = _factory(tmp_path)
    source, high_cap_run = _run_with_items(sf, ["one", "two"])
    provider = _Provider()

    screened = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=high_cap_run,
        limit=1000,
    )
    analyzed = run_stage_b_analysis_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=high_cap_run,
        limit=1000,
    )
    assert screened.partial is False
    assert screened.partial_reason is None
    assert analyzed.partial is False
    assert analyzed.partial_reason is None

    _, capped_screen_run = _run_with_items(sf, ["screen-one", "screen-two"])
    capped_screen = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=capped_screen_run,
        limit=1,
    )
    assert capped_screen.partial is True
    assert capped_screen.partial_reason == "ai_limit:1"
    with sf() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(capped_screen_run, "screen")
        assert stage is not None and stage.status == "pending"
        assert sorted(task.status for task in repo.list_stage_tasks(stage, subject_type="item")) == ["pending", "succeeded"]
    resumed_screen = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=capped_screen_run,
        limit=1,
    )
    assert resumed_screen.processed == 1
    assert resumed_screen.partial is False
    with sf() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(capped_screen_run, "screen")
        assert stage is not None and stage.status == "succeeded"
        assert all(task.status == "succeeded" for task in repo.list_stage_tasks(stage, subject_type="item"))

    _, capped_analysis_run = _run_with_items(sf, ["analysis-one", "analysis-two"])
    run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=capped_analysis_run,
        limit=None,
    )
    capped_analysis = run_stage_b_analysis_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=capped_analysis_run,
        limit=1,
    )
    assert capped_analysis.partial is True
    assert capped_analysis.partial_reason == "ai_limit:1"
    with sf() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(capped_analysis_run, "analyze")
        assert stage is not None and stage.status == "pending"
        assert sorted(task.status for task in repo.list_stage_tasks(stage, subject_type="item")) == ["pending", "succeeded"]
    resumed_analysis = run_stage_b_analysis_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=capped_analysis_run,
        limit=1,
    )
    assert resumed_analysis.processed == 1
    assert resumed_analysis.partial is False
    with sf() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(capped_analysis_run, "analyze")
        assert stage is not None and stage.status == "succeeded"
        assert all(task.status == "succeeded" for task in repo.list_stage_tasks(stage, subject_type="item"))


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


def test_provider_retryable_failure_is_terminal_after_automatic_retries(tmp_path):
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
        assert [task.status for task in tasks] == ["blocked", "blocked"]
        assert tasks[0].error_category == "provider_retry_exhausted"
        assert "after 6 attempts" in (tasks[0].error_message or "")
        assert len(provider.screen_calls) == 7


def test_provider_concurrency_is_bounded_per_daily_stage(tmp_path):
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
    stage_a = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        concurrency=2,
    )
    stage_b = run_stage_b_analysis_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        concurrency=2,
    )

    assert stage_a.screened == 8
    assert stage_b.candidate == 8
    assert provider.max_active > 1
    assert provider.max_active <= 2


def test_stage_a_claims_only_inflight_tasks_before_short_lease_expires(tmp_path, monkeypatch):
    """Queued work must not inherit a lease that expires before execution."""

    sf = _factory(tmp_path)
    titles = [f"queued-{index}" for index in range(30)]
    source, run_id = _run_with_items(sf, titles)

    original_claim = IntelRepository.claim_stage_task

    def short_lease_claim(self, *args, **kwargs):
        # Repository clamps leases to at least one second.  Thirty provider
        # calls at 80ms with two workers take long enough to expose the old
        # claim-all queue, while each bounded in-flight call remains live.
        kwargs["lease_seconds"] = 1
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(IntelRepository, "claim_stage_task", short_lease_claim)

    class SlowProvider(_Provider):
        def screen(self, envelope):
            time.sleep(0.08)
            return super().screen(envelope)

    provider = SlowProvider()
    result = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=provider,
        run_id=run_id,
        limit=None,
        concurrency=2,
    )

    assert result.screened == len(titles)
    assert result.screen_failed == 0
    with sf() as session:
        stage = IntelRepository(session).get_stage(run_id, "screen")
        tasks = IntelRepository(session).list_stage_tasks(stage, subject_type="item", include_expired=True)
        assert len(tasks) == len(titles)
        assert all(task.status == "succeeded" for task in tasks)


def test_stage_a_expired_lease_does_not_publish_stale_projection(tmp_path, monkeypatch):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["expires"])

    original_claim = IntelRepository.claim_stage_task

    def one_second_lease_claim(self, *args, **kwargs):
        kwargs["lease_seconds"] = 1
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(IntelRepository, "claim_stage_task", one_second_lease_claim)

    class ExpiringProvider(_Provider):
        def screen(self, envelope):
            time.sleep(1.1)
            return super().screen(envelope)

    result = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ExpiringProvider(),
        run_id=run_id,
        limit=None,
        concurrency=1,
    )

    assert result.screened == 0
    assert result.screen_failed == 0
    assert result.skipped == 1
    with sf() as session:
        stage = IntelRepository(session).get_stage(run_id, "screen")
        task = session.scalar(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id))
        assert task is not None
        assert task.status == "retry_waiting"
        assert session.scalar(select(AIItemScreen).where(AIItemScreen.item_id == 1)) is None


def test_stage_a_expired_failure_lease_is_retryable_without_failure_projection(tmp_path, monkeypatch):
    sf = _factory(tmp_path)
    source, run_id = _run_with_items(sf, ["expires-failure"])

    original_claim = IntelRepository.claim_stage_task

    def one_second_lease_claim(self, *args, **kwargs):
        kwargs["lease_seconds"] = 1
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(IntelRepository, "claim_stage_task", one_second_lease_claim)

    class ExpiringFailureProvider(_Provider):
        def screen(self, envelope):
            time.sleep(1.1)
            raise RuntimeError("provider timeout")

    result = run_stage_a_screen_job(
        session_factory=sf,
        source_specs={source.id: source},
        ai_client=ExpiringFailureProvider(),
        run_id=run_id,
        limit=None,
        concurrency=1,
    )

    assert result.screen_failed == 0
    assert result.skipped == 1
    with sf() as session:
        stage = IntelRepository(session).get_stage(run_id, "screen")
        task = session.scalar(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id))
        assert task is not None
        assert task.status == "retry_waiting"
        assert session.scalar(select(AIItemScreen).where(AIItemScreen.item_id == 1)) is None


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
