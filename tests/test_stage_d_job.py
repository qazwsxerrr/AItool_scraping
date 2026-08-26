from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select

from app.ai.skills.stage_d_selection import (
    STAGE_D_SELECTION_PROMPT_VERSION,
    STAGE_D_SELECTION_SCHEMA_VERSION,
    STAGE_D_SELECTION_SYSTEM_PROMPT,
    build_stage_d_provider_payload,
    strict_parse_stage_d_selection,
)
from app.ai.tavily import TavilySearchResponse, TavilySearchResult
from app.jobs.stage_d_job import StageDExecutionError, StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelAgentSession, IntelAgentStep, IntelEvent, IntelEventEvidence, IntelRunStageAttempt
from app.storage.repository import IntelRepository


REFERENCE = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def test_stage_d_prompt_reviews_stage_c_candidates_against_daily_requirements():
    prompt = STAGE_D_SELECTION_SYSTEM_PROMPT

    assert STAGE_D_SELECTION_PROMPT_VERSION == "stage_d_editorial_review_v13"
    assert "Stage C 已完成时间准入" in prompt
    assert "editorial_caveats" in prompt
    assert "模型、API 和开发工具" in prompt
    assert "AI 基础设施、芯片、算力、产业链和研发突破" in prompt
    assert "event_family_key" in prompt
    assert "publishability" in prompt
    assert "不要只因标题命中某些词就保留" in prompt
    assert "priority_tier" not in prompt
    assert "editorial_lane" not in prompt
    assert "soft_selected_target" in prompt
    assert "每个 candidate_events 中的 event_id 必须且只能出现在 selected 或 unselected 之一" in prompt
    assert "eligibility_blockers" in prompt
    assert "同一故事族不要连续占满头部" in prompt

    payload = build_stage_d_provider_payload([], max_selected=30)
    assert payload["input"][0]["content"] == prompt


def test_stage_d_input_preserves_editorial_caveats_for_research_and_eval_events():
    prompt = STAGE_D_SELECTION_SYSTEM_PROMPT
    assert "editorial_caveats" in prompt
    assert "绝不能作为淘汰或降级事件的理由" in prompt


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return engine, create_session_factory(engine)


def _build(
    session_factory,
    *,
    event_count: int = 3,
    candidate_indexes: list[int] | None = None,
    needs_review_indexes: list[int] | None = None,
    with_cluster: bool = True,
) -> tuple[int, list[int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(
            edition_date="2026-08-19",
            reference_time=REFERENCE,
        )
        event_ids: list[int] = []
        needs_review = set(needs_review_indexes or ())
        for index in range(event_count):
            event = repo.upsert_event(
                run_id=build.id,
                event_key=f"event:{index}",
                canonical_url=f"https://example.test/events/{index}",
                title=f"Stage C 标题 {index}",
                summary_cn=f"Stage C 摘要 {index}",
                topic="model_release",
                topics=["model_release"],
                keywords=["模型", f"能力{index}"],
                entities=[{"name": f"Model {index}", "type": "product"}],
                content_class="official_model_company",
                source_group="official_blog",
                source_ids=["official-source"],
                source_groups=["official_blog"],
                display_score=90 - index,
                novelty_status="new",
                state="candidate",
                review_state="needs_review" if index in needs_review else "candidate",
                first_seen_at=REFERENCE,
                last_seen_at=REFERENCE,
            )
            event_ids.append(int(event.id))
        if with_cluster:
            candidates = (
                [event_ids[index] for index in candidate_indexes]
                if candidate_indexes is not None
                else event_ids
            )
            cluster = repo.ensure_stage(build.id, "cluster")
            task = repo.ensure_stage_task(
                cluster,
                subject_type="run",
                subject_id=build.id,
                target_run_id=build.id,
            )
            repo.complete_stage_task(
                task,
                result={
                    "current_event_ids": event_ids,
                    "candidate_event_ids": candidates,
                },
            )
            repo.finish_stage(cluster, status="succeeded")
        session.commit()
        return int(build.id), event_ids


def _update_event_for_stage_d(
    session_factory,
    event_id: int,
    *,
    title: str,
    summary_cn: str,
    topic: str,
    display_score: float,
    event_family_key: str,
    keywords: list[str] | None = None,
    topics: list[str] | None = None,
    content_class: str = "official_model_company",
    review_state: str = "candidate",
) -> None:
    with session_factory() as session:
        event = session.get(IntelEvent, int(event_id))
        assert event is not None
        event.title = title
        event.summary_cn = summary_cn
        event.topic = topic
        event.topics_json = json.dumps(topics or [topic])
        event.keywords_json = json.dumps(keywords or [])
        event.content_class = content_class
        event.source_groups_json = json.dumps(["official_blog"])
        event.display_score = display_score
        event.review_state = review_state
        event.resolution_raw_json = json.dumps(
            {
                "draft_metadata": {
                    "event_family_key": event_family_key,
                    "facts": [{"claim": summary_cn, "supporting_item_ids": [int(event_id)]}],
                    "publishability": review_state,
                    "history_status": "new",
                    "split_reason": None,
                    "caveats": [],
                }
            }
        )
        session.commit()


class _SelectionClient:
    model = "stage-d-selection-test"
    transport = "responses"
    max_retries = 0
    timeout_seconds = 1

    def __init__(self, selected_indexes: list[int] | None = None, *, payload=None, error=None):
        self.selected_indexes = selected_indexes
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    def select(self, events, *, edition, max_selected):
        self.calls.append(
            {
                "events": [dict(event) for event in events],
                "edition": dict(edition),
                "max_selected": max_selected,
            }
        )
        if self.error is not None:
            raise self.error
        if self.payload is not None:
            return self.payload
        indexes = self.selected_indexes
        selected_events = list(events) if indexes is None else [events[index] for index in indexes]
        return {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {
                    "event_id": int(event["event_id"]),
                    "reason_code": "daily_value",
                    "reason": f"事件 {event['event_id']} 对本期读者有明确价值。",
                }
                for event in selected_events
            ],
            "unselected": [
                {
                    "event_id": int(event["event_id"]),
                    "reason_code": "insufficient_value",
                    "reason": f"事件 {event['event_id']} 未入选。",
                }
                for event in events
                if event not in selected_events
            ],
        }


class _SearchClient:
    is_configured = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query, *, topic="general", max_results=5, **_kwargs):
        self.calls.append(query)
        index = len(self.calls)
        return TavilySearchResponse(
            query=query,
            request_id=f"request-{index}",
            response_time=0.1,
            results=(
                TavilySearchResult(
                    result_id=f"search-result-{index:03d}",
                    title=f"核验来源 {index}",
                    url=f"https://verify.example/{index}",
                    content="公开来源提供了该事件的核验线索。",
                    score=0.9,
                    published_date="2026-08-19",
                ),
            ),
            usage={"credits": 1},
            raw_response={},
        )


def test_selection_schema_accepts_only_an_ordered_candidate_subset():
    parsed = strict_parse_stage_d_selection(
        {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {"event_id": 3, "reason_code": "high_impact", "reason": "影响范围明确。"},
                {"event_id": 1, "reason_code": "actionable", "reason": "读者可立即使用。"},
            ],
            "unselected": [
                {"event_id": 2, "reason_code": "low_value", "reason": "淘汰。"},
            ],
        },
        candidate_event_ids=[1, 2, 3],
        max_selected=2,
    )

    assert [row.event_id for row in parsed.selected] == [3, 1]

    with pytest.raises(ValueError, match="unknown candidate"):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [{"event_id": 4, "reason_code": "impact", "reason": "未知事件。"}],
                "unselected": [],
            },
            candidate_event_ids=[1, 2, 3],
            max_selected=2,
        )
    with pytest.raises(ValueError, match="did not return decisions"):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [{"event_id": 1, "reason_code": "impact", "reason": "选择。"}],
                "unselected": [],
            },
            candidate_event_ids=[1, 2],
            max_selected=2,
        )
    with pytest.raises(ValueError, match="duplicate event_id"):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {"event_id": 1, "reason_code": "impact", "reason": "理由一。"},
                    {"event_id": 1, "reason_code": "impact", "reason": "理由二。"},
                ],
                "unselected": [],
            },
            candidate_event_ids=[1, 2, 3],
            max_selected=2,
        )
    with pytest.raises(ValueError, match="exceeding max_selected"):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {"event_id": 1, "reason_code": "impact", "reason": "理由一。"},
                    {"event_id": 2, "reason_code": "impact", "reason": "理由二。"},
                ],
                "unselected": [],
            },
            candidate_event_ids=[1, 2, 3],
            max_selected=1,
        )
    with pytest.raises(ValueError):
        strict_parse_stage_d_selection(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {
                        "event_id": 1,
                        "reason_code": "impact",
                        "reason": "理由。",
                        "display_title_zh": "Stage D 不得改标题",
                    }
                ],
                "unselected": [],
            },
            candidate_event_ids=[1],
            max_selected=1,
        )


def test_stage_d_requires_a_successful_stage_c_candidate_contract():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, with_cluster=False)

    with pytest.raises(StageDExecutionError, match="Stage C cluster stage must succeed"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=_SelectionClient(),
        )

    with session_factory() as session:
        assert IntelRepository(session).get_stage(run_id, "stage_d") is None


def test_empty_stage_c_candidates_finish_without_calling_the_provider():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, event_count=0)
    client = _SelectionClient(error=AssertionError("provider must not be called"))

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
    )

    assert (result.candidates, result.selected, result.unselected) == (0, 0, 0)
    assert client.calls == []
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "succeeded"
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None
        assert task.result == {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "candidate_event_ids": [],
            "withheld_needs_review_event_ids": [],
            "all_stage_c_candidate_event_ids": [],
            "selected": [],
            "unselected": [],
            "input_fingerprint": task.input_fingerprint,
            "config_fingerprint": task.config_fingerprint,
            "provider_attempts": 0,
            "web_searches": 0,
            "agent_session_id": 1,
        }


def test_stage_d_persists_only_the_ordered_selection_task_result():
    engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, candidate_indexes=[2, 0])
    client = _SelectionClient(selected_indexes=[1])

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
    )

    expected_candidates = [event_ids[2], event_ids[0]]
    assert (result.candidates, result.selected, result.unselected) == (2, 1, 1)
    assert [row["event_id"] for row in client.calls[0]["events"]] == expected_candidates
    assert client.calls[0]["max_selected"] == 30
    assert "intel_event_stage_d_snapshots" not in inspect(engine).get_table_names()
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "succeeded"
        tasks = repo.list_stage_tasks(stage, include_expired=True)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.result["candidate_event_ids"] == expected_candidates
        assert task.result["selected"] == [
            {
                "event_id": event_ids[0],
                "reason_code": "daily_value",
                "reason": f"事件 {event_ids[0]} 对本期读者有明确价值。",
            }
        ]
        assert task.result["unselected"] == [
            {
                "event_id": event_ids[2],
                "reason_code": "insufficient_value",
                "reason": f"事件 {event_ids[2]} 未入选。",
            }
        ]
        assert set(task.result) == {
            "schema_version",
            "candidate_event_ids",
            "withheld_needs_review_event_ids",
            "all_stage_c_candidate_event_ids",
            "selected",
            "unselected",
            "input_fingerprint",
            "config_fingerprint",
            "provider_attempts",
            "web_searches",
            "agent_session_id",
        }
        event = session.get(IntelEvent, event_ids[0])
        assert event is not None and event.title == "Stage C 标题 0"
        attempts = repo.list_stage_attempts(task)
        assert len(attempts) == 1
        assert isinstance(attempts[0], IntelRunStageAttempt)
        assert attempts[0].raw_response["schema_version"] == STAGE_D_SELECTION_SCHEMA_VERSION


def test_stage_d_sends_needs_review_events_to_the_final_reviewer():
    _engine, session_factory = _db()
    run_id, event_ids = _build(
        session_factory,
        event_count=2,
        needs_review_indexes=[1],
    )
    client = _SelectionClient()

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
    )

    assert (result.candidates, result.withheld_needs_review, result.selected) == (2, 0, 2)
    assert [row["event_id"] for row in client.calls[0]["events"]] == event_ids
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None
        assert task.result["candidate_event_ids"] == event_ids
        assert task.result["withheld_needs_review_event_ids"] == []
        assert task.result["all_stage_c_candidate_event_ids"] == event_ids


def test_stage_d_leaves_intent_only_event_judgment_to_the_final_reviewer():
    _engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, event_count=2, needs_review_indexes=[0])
    with session_factory() as session:
        event = session.get(IntelEvent, event_ids[0])
        assert event is not None
        event.risk_flags_json = json.dumps([])
        event.resolution_raw_json = json.dumps(
            {
                "draft_metadata": {
                    "event_family_key": "nvidia_vera_rubin_infra_push",
                    "facts": [
                        {
                            "claim": "公司宣布将优化组织并引进人才。",
                            "supporting_item_ids": [1],
                        },
                        {
                            "claim": "公司将持续投入以提升竞争力。",
                            "supporting_item_ids": [1],
                        },
                    ],
                    "publishability": "needs_review",
                    "history_status": "new",
                    "split_reason": None,
                    "caveats": [],
                }
            }
        )
        session.commit()

    client = _SelectionClient()
    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert (result.candidates, result.selected, result.unselected) == (2, 2, 0)
    assert [row["event_id"] for row in client.calls[0]["events"]] == event_ids
    assert client.calls[0]["events"][0]["event_family_key"] == "nvidia_vera_rubin_infra_push"
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None
        assert [row["event_id"] for row in task.result["selected"]] == event_ids
        attempts = repo.list_stage_attempts(task)
        assert len(attempts) == 1
        assert "selection_guard" not in attempts[0].metadata_dict


def test_stage_d_input_uses_stage_c_event_package_fields_without_local_priority_fields():
    _engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, event_count=2)
    _update_event_for_stage_d(
        session_factory,
        event_ids[0],
        title="OpenAI将GPT-5.6 API价格下调",
        summary_cn="OpenAI宣布GPT-5.6 Sol API价格下调，并影响Codex Credits可用调用量。",
        topic="developer_ecosystem",
        display_score=70,
        event_family_key="openai_api_price_cut",
        keywords=["OpenAI", "GPT-5.6", "API", "价格", "Codex Credits"],
    )
    _update_event_for_stage_d(
        session_factory,
        event_ids[1],
        title="NVIDIA推出NVLink Fusion连接定制XPU",
        summary_cn="NVIDIA推出NVLink Fusion，将定制XPU接入AI工厂基础设施。",
        topic="technology_insight",
        display_score=88,
        event_family_key="nvidia_nvlink_fusion",
        keywords=["NVIDIA", "NVLink Fusion", "XPU", "AI工厂"],
    )
    client = _SelectionClient()

    run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    events = client.calls[0]["events"]
    price_cut = next(row for row in events if row["event_id"] == event_ids[0])
    infra = next(row for row in events if row["event_id"] == event_ids[1])
    assert price_cut["event_family_key"] == "openai_api_price_cut"
    assert infra["event_family_key"] == "nvidia_nvlink_fusion"
    assert price_cut["publishability"] == "candidate"
    assert price_cut["facts"]
    assert "priority_tier" not in price_cut
    assert "editorial_lane" not in price_cut
    assert "priority_policy" not in price_cut
    assert "priority_tier" not in infra
    assert "editorial_lane" not in infra
    assert "priority_policy" not in infra


def test_stage_d_persists_provider_order_without_local_reordering():
    _engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, event_count=3)
    _update_event_for_stage_d(
        session_factory,
        event_ids[0],
        title="NVIDIA披露Vera Rubin智能体推理能效数据",
        summary_cn="NVIDIA披露Vera Rubin NVL72在智能体工作负载上的吞吐与成本数据。",
        topic="technology_insight",
        display_score=95,
        event_family_key="nvidia_vera_rubin",
        keywords=["NVIDIA", "Vera Rubin", "GB300", "吞吐"],
    )
    _update_event_for_stage_d(
        session_factory,
        event_ids[1],
        title="GPT-5.6接入Kiro开发工作流",
        summary_cn="OpenAI宣布GPT-5.6已在Kiro可用，可用于软件规划、构建、测试和审查。",
        topic="developer_ecosystem",
        display_score=68,
        event_family_key="gpt56_kiro",
        keywords=["OpenAI", "GPT-5.6", "Kiro", "可用", "开发工具"],
    )
    _update_event_for_stage_d(
        session_factory,
        event_ids[2],
        title="阿里云Token Plan接入千问App及PC端",
        summary_cn="阿里云Token Plan接入千问App及PC端，绑定API Key后可调用订阅额度。",
        topic="developer_ecosystem",
        display_score=79,
        event_family_key="qwen_token_plan",
        keywords=["阿里云", "Token Plan", "API Key", "订阅", "额度", "千问"],
    )
    client = _SelectionClient(selected_indexes=[0, 1, 2])

    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert result.selected == 3
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None
        assert [row["event_id"] for row in task.result["selected"]] == [
            event_ids[0],
            event_ids[1],
            event_ids[2],
        ]
        attempts = repo.list_stage_attempts(task)
        assert "selection_guard" not in attempts[0].metadata_dict


def test_stage_d_does_not_trim_provider_selection_to_the_soft_target():
    _engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, event_count=25)
    for index, event_id in enumerate(event_ids):
        _update_event_for_stage_d(
            session_factory,
            event_id,
            title=f"生态补充事件 {index}",
            summary_cn=f"生态补充事件 {index} 的摘要。",
            topic="industry_dynamics",
            display_score=50 + index,
            event_family_key=f"community_fixture_{index}",
            keywords=["生态", "补充"],
            content_class="community_social",
        )
    _update_event_for_stage_d(
        session_factory,
        event_ids[0],
        title="OpenAI Developers推出按API Key追踪用量与支出",
        summary_cn="OpenAI Developers上线按API Key追踪用量与支出，并支持月度硬性限额。",
        topic="developer_ecosystem",
        display_score=74,
        event_family_key="openai_api_key_usage",
        keywords=["OpenAI", "API Key", "用量", "支出", "限额"],
    )
    _update_event_for_stage_d(
        session_factory,
        event_ids[1],
        title="NVIDIA推出NVLink Fusion连接定制XPU",
        summary_cn="NVIDIA推出NVLink Fusion，将定制XPU接入AI工厂基础设施。",
        topic="technology_insight",
        display_score=90,
        event_family_key="nvidia_nvlink_fusion",
        keywords=["NVIDIA", "NVLink Fusion", "XPU", "AI工厂"],
    )
    client = _SelectionClient()

    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, ai_client=client)

    assert result.selected == 25
    assert result.unselected == 0
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None
        selected_ids = [row["event_id"] for row in task.result["selected"]]
        assert selected_ids == event_ids
        attempts = repo.list_stage_attempts(task)
        assert "selection_guard" not in attempts[0].metadata_dict


def test_stage_d_tavily_sources_are_passed_to_review_and_persisted_for_audit():
    _engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, event_count=2, needs_review_indexes=[1])
    client = _SelectionClient(selected_indexes=[1])
    search = _SearchClient()

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        search_client=search,
        max_web_searches=1,
    )

    assert result.web_searches == 1
    assert len(search.calls) == 1
    reviewed = client.calls[0]["events"]
    needs_review = next(row for row in reviewed if row["event_id"] == event_ids[1])
    assert needs_review["search_status"] == "searched"
    assert needs_review["search_evidence"][0]["url"] == "https://verify.example/1"
    with session_factory() as session:
        agent = session.scalar(select(IntelAgentSession).where(IntelAgentSession.stage_name == "stage_d"))
        assert agent is not None and agent.web_search_count == 1
        steps = session.scalars(select(IntelAgentStep).where(IntelAgentStep.session_id == agent.id)).all()
        assert [step.tool_name for step in steps] == ["search_web"]
        evidence = session.scalars(select(IntelEventEvidence).where(IntelEventEvidence.session_id == agent.id)).all()
        assert len(evidence) == 1
        assert evidence[0].event_id == event_ids[1]
        assert evidence[0].source_scope == "tavily"


def test_stage_d_keeps_tavily_audit_when_the_editorial_provider_fails():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, event_count=1, needs_review_indexes=[0])

    with pytest.raises(StageDExecutionError, match="provider unavailable"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=_SelectionClient(error=ValueError("provider unavailable")),
            search_client=_SearchClient(),
            max_web_searches=1,
        )

    with session_factory() as session:
        agent = session.scalar(select(IntelAgentSession).where(IntelAgentSession.stage_name == "stage_d"))
        assert agent is not None and agent.status == "failed"
        assert session.scalars(select(IntelAgentStep).where(IntelAgentStep.session_id == agent.id)).all()
        assert session.scalars(select(IntelEventEvidence).where(IntelEventEvidence.session_id == agent.id)).all()


def test_invalid_selection_is_blocked_without_local_fallback():
    _engine, session_factory = _db()
    run_id, event_ids = _build(session_factory, event_count=1)
    client = _SelectionClient(
        payload={
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {
                    "event_id": event_ids[0],
                    "reason_code": "impact",
                    "reason": "选择该事件。",
                    "title": "Stage D 试图覆盖 Stage C 标题",
                }
            ],
        }
    )

    with pytest.raises(StageDExecutionError, match="selection"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=client,
        )

    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "stage_d")
        assert stage is not None and stage.status == "blocked"
        task = repo.get_task(stage, subject_type="run", subject_id=run_id)
        assert task is not None and task.status == "blocked"
        assert task.result.get("selected") is None
        attempts = repo.list_stage_attempts(task)
        assert attempts[0].raw_response["selected"][0]["title"] == "Stage D 试图覆盖 Stage C 标题"


def test_profile_enforces_the_only_stage_d_limit():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, event_count=2)

    with pytest.raises(StageDExecutionError, match="max_selected=1"):
        run_stage_d_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=_SelectionClient(),
            profile=StageDProfile(max_selected=1),
        )


def test_zero_selection_limit_finishes_without_calling_the_provider():
    _engine, session_factory = _db()
    run_id, _ = _build(session_factory, event_count=2)
    client = _SelectionClient(error=AssertionError("provider must not be called"))

    result = run_stage_d_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=client,
        profile=StageDProfile(max_selected=0),
    )

    assert (result.candidates, result.selected, result.unselected) == (2, 0, 2)
    assert client.calls == []
