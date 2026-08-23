from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest
from sqlalchemy import select

from app.ai.responses import AgentBudgetExceeded, AgentProtocolError, AgentRunResult
from app.ai.tavily import TavilySearchResponse, TavilySearchResult
from app.domain.models import FetchItem
from app.jobs.event_cluster_job import _primary_sort_key, _stage_c_lease_seconds, run_event_cluster_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelAgentSession, IntelEvent, IntelEventEvidence, IntelItem, IntelRunStageTask, Source
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


class _SearchClient:
    is_configured = True

    def __init__(self, *, url: str = "https://source.example/verified", empty: bool = False) -> None:
        self.url = url
        self.empty = empty
        self.calls: list[dict[str, Any]] = []

    def search(self, query, *, topic="general", max_results=5, **_kwargs):
        self.calls.append({"query": query, "topic": topic, "max_results": max_results})
        rows = () if self.empty else (
            TavilySearchResult(
                result_id="result-verified-001",
                title="Verification source",
                url=self.url,
                content="The source confirms the launch.",
                score=0.95,
                published_date="2026-08-19",
            ),
        )
        return TavilySearchResponse(
            query=query,
            request_id="tavily-request-1",
            response_time=0.1,
            results=rows,
            usage={"credits": 1},
            raw_response={},
        )


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def test_stage_c_primary_source_uses_group_and_content_class_only():
    official = IntelItem(
        id=1, build_id=1, source_id="official", title="Official", content_hash="a",
        content_class="official_model_company", b1_priority=70, captured_at=NOW,
        source=Source(
            id="official", name="Official", transport="feed", url="https://official.example/feed.xml",
            source_group="official_blog", content_class="official_model_company",
        ),
    )
    media = IntelItem(
        id=2, build_id=1, source_id="media", title="Media", content_hash="b",
        content_class="news_media", b1_priority=99, captured_at=NOW,
        source=Source(
            id="media", name="Media", transport="feed", url="https://media.example/feed.xml",
            source_group="tech_media", content_class="news_media",
        ),
    )

    assert _primary_sort_key(official) < _primary_sort_key(media)


def _seed_build(
    session_factory,
    *,
    rows: list[tuple[str, int, str]],
    edition_date: str = "2026-08-19",
) -> tuple[int, dict[str, int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        session.add(
            Source(
                id="agent-source",
                name="Agent source",
                transport="feed",
                url="https://source.example/feed.xml",
                source_group="official_blog",
                content_class="official_model_company",
            )
        )
        session.flush()
        _, run = repo.start_daily_build(edition_date=edition_date, reference_time=NOW)
        stage = repo.ensure_stage(run.id, "analyze")
        ids: dict[str, int] = {}
        admissions: list[dict[str, Any]] = []
        for index, (title, score, decision) in enumerate(rows, start=1):
            inserted = repo.insert_item(
                FetchItem(
                    source_id="agent-source",
                    external_id=f"agent-{index}",
                    title=title,
                    summary=f"{title} 的摘要",
                    content_text=f"{title} 的完整正文，含有可聚合的发布信息。",
                    url=f"https://source.example/{index}",
                    content_class="official_model_company",
                    published_at=NOW,
                    captured_at=NOW,
                ),
                run_id=run.id,
            )
            assert inserted.item_id is not None
            item_id = int(inserted.item_id)
            ids[title] = item_id
            session.add(
                AIItemReview(
                    item_id=item_id,
                    content_class="official_model_company",
                    topic="model_release",
                    topics_json='["model_release"]',
                    keywords_json=json.dumps([title, "release"]),
                    entities_json='[{"name":"Acme","type":"company","aliases":[]}]',
                    summary_cn=f"{title} 的分析摘要",
                    b1_priority=score,
                    score_components_json='{}',
                    status="success",
                )
            )
            task = repo.ensure_stage_task(stage, subject_type="item", subject_id=item_id, item_id=item_id)
            repo.complete_stage_task(task, result={"item_id": item_id, "b1_priority": score})
            admissions.append(
                {
                    "item_id": item_id,
                    "decision": decision,
                    "rank": index if decision != "filtered" else None,
                    "guarded_score": score,
                    "reason_code": "fixture",
                    "reason": "fixture admission",
                    "policy_version": "fixture-v1",
                    "policy_fingerprint": "fixture-policy",
                }
            )
        repo.replace_candidate_admissions(run.id, admissions)
        repo.finish_stage(stage, status="succeeded")
        repo.freeze_run_scope(run.id)
        session.commit()
        return int(run.id), ids


class _ToolAgent:
    model = "stage-c-agent-fixture"
    transport = "responses"
    timeout_seconds = 30.0

    def __init__(
        self,
        *,
        web_evidence: bool = False,
        bad_evidence: bool = False,
        source_less_web: bool = False,
        opened_page_evidence: bool = False,
        intent_only: bool = False,
        repeat: bool = False,
    ) -> None:
        self.web_evidence = web_evidence
        self.bad_evidence = bad_evidence
        self.source_less_web = source_less_web
        self.opened_page_evidence = opened_page_evidence
        self.intent_only = intent_only
        self.repeat = repeat
        self.contexts: list[dict[str, Any]] = []

    def run(self, *, initial_context, function_tools, on_response, on_tool, **_kwargs):
        self.contexts.append(dict(initial_context))
        tools = {tool.name: tool for tool in function_tools}
        turn = 1
        output: list[dict[str, Any]] = []
        on_response(turn, {"id": "fixture-response", "output": output})
        calls = 0

        def invoke(name: str, arguments: Mapping[str, Any]):
            nonlocal calls
            calls += 1
            call = {"name": name, "call_id": f"call-{calls}", "arguments": json.dumps(arguments)}
            result = dict(tools[name].handler(dict(arguments)))
            on_tool(turn, call, result)
            return result

        active: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = invoke("list_candidates", {"bucket": "active", "offset": offset, "limit": 30})
            rows = list(page.get("items") or [])
            active.extend(rows)
            offset += len(rows)
            if offset >= int(page.get("total") or 0):
                break
        drafts = [
            {
                "draft_key": f"draft-{item['id']}",
                "item_ids": [item["id"]],
                "title": item["title"],
                "summary_cn": item.get("summary_cn") or item["title"],
                "topic": item.get("topic") or "technology_insight",
                "topics": [item.get("topic") or "technology_insight"],
                "keywords": item.get("keywords") or [],
                "entities": item.get("entities") or [],
                "event_action": "release",
                "lifecycle_state": "ga",
                "aggregation_basis": [],
                "novelty_status": "repeat" if self.repeat else "new",
                "prior_event_key": f"url:{item['canonical_url']}" if self.repeat else None,
                "novelty_reason": "当前候选与近三期事件相同且没有实质变化。" if self.repeat else "当前候选未匹配近三期已发布日报。",
                "material_changes": [],
                "substance_status": "intent_only" if self.intent_only else "concrete",
                "substantive_facts": [] if self.intent_only else [
                    {
                        "fact_type": "product",
                        "claim": "候选正文确认产品已正式发布。",
                        "supporting_item_ids": [item["id"]],
                    }
                ],
                "review_state": "candidate",
                "confidence": 88,
                "risk_flags": [],
            }
            for item in active
        ]
        for offset in range(0, len(drafts), 8):
            saved = invoke("save_event_drafts", {"drafts": drafts[offset : offset + 8]})
            assert saved["ok"] is True
        if active and (self.web_evidence or self.bad_evidence):
            searched = invoke(
                "search_web",
                {
                    "draft_key": f"draft-{active[0]['id']}",
                    "query": active[0]["title"],
                    "claim": "确认发布动作",
                    "topic": "news",
                    "max_results": 5,
                },
            )
            if self.bad_evidence or searched.get("results"):
                invoke(
                    "attach_search_evidence",
                    {
                        "draft_key": f"draft-{active[0]['id']}",
                        "result_id": "not-returned-result" if self.bad_evidence else searched["results"][0]["result_id"],
                        "claim": "确认发布动作",
                        "verdict": "supports",
                    },
                )
        final = invoke("finalize_event_drafts", {})
        assert final["ok"] is True
        return AgentRunResult(
            response_id="fixture-response",
            turns=turn,
            tool_calls=calls,
            web_searches=1 if self.web_evidence else 0,
            finalized=True,
            last_response={"id": "fixture-response", "output": output},
        )


class _BudgetAgent:
    model = "stage-c-budget-fixture"
    transport = "responses"

    def run(self, *, on_response, **_kwargs):
        on_response(1, {"id": "budget-response", "output": []})
        raise AgentBudgetExceeded("fixture budget exhausted")


class _InvalidCoverageAgent:
    model = "stage-c-invalid-fixture"
    transport = "responses"

    def run(self, *, function_tools, on_response, on_tool, **_kwargs):
        tools = {tool.name: tool for tool in function_tools}
        on_response(1, {"id": "invalid-response", "output": []})
        call = {"name": "finalize_event_drafts", "call_id": "invalid-finalize", "arguments": "{}"}
        result = dict(tools["finalize_event_drafts"].handler({}))
        on_tool(1, call, result)
        return AgentRunResult("invalid-response", 1, 1, 0, False, {"id": "invalid-response", "output": []})


class _ReviewFlowAgent:
    model = "stage-c-review-flow-fixture"
    transport = "responses"

    def __init__(self, *, unresolved: bool = False) -> None:
        self.unresolved = unresolved
        self.first_finalize: dict[str, Any] | None = None

    def run(self, *, function_tools, on_response, on_tool, **_kwargs):
        tools = {tool.name: tool for tool in function_tools}
        calls = 0

        def invoke(turn: int, name: str, arguments: Mapping[str, Any]):
            nonlocal calls
            calls += 1
            call = {"name": name, "call_id": f"review-{calls}", "arguments": json.dumps(arguments)}
            result = dict(tools[name].handler(dict(arguments)))
            on_tool(turn, call, result)
            return result

        on_response(1, {"id": "review-initial", "output": []})
        page = invoke(1, "list_candidates", {"bucket": "active", "offset": 0, "limit": 30})
        item = page["items"][0]
        draft = {
            "draft_key": f"review-{item['id']}",
            "item_ids": [item["id"]],
            "title": item["title"],
            "summary_cn": item.get("summary_cn") or item["title"],
            "topic": item.get("topic") or "technology_insight",
            "topics": [item.get("topic") or "technology_insight"],
            "keywords": item.get("keywords") or [],
            "entities": item.get("entities") or [],
            "event_action": "release",
            "lifecycle_state": "ga",
            "aggregation_basis": [],
            "novelty_status": "new",
            "prior_event_key": None,
            "novelty_reason": "当前候选未匹配近三期已发布日报。",
            "material_changes": [],
            "substance_status": "concrete",
            "substantive_facts": [
                {
                    "fact_type": "product",
                    "claim": "候选正文确认产品已正式发布。",
                    "supporting_item_ids": [item["id"]],
                }
            ],
            "review_state": "needs_review",
            "confidence": 80,
            "risk_flags": ["claim_requires_confirmation"],
        }
        assert invoke(1, "save_event_drafts", {"drafts": [draft]})["ok"] is True
        self.first_finalize = invoke(1, "finalize_event_drafts", {})
        assert self.first_finalize["ok"] is False

        searched = invoke(
            2,
            "search_web",
            {
                "draft_key": draft["draft_key"],
                "query": item["title"],
                "claim": "确认发布动作",
                "topic": "news",
                "max_results": 5,
            },
        )
        on_response(2, {"id": "review-search", "output": []})
        if not self.unresolved and searched.get("results"):
            evidence = invoke(
                2,
                "attach_search_evidence",
                {
                    "draft_key": draft["draft_key"],
                    "result_id": searched["results"][0]["result_id"],
                    "claim": "确认发布动作",
                    "verdict": "supports",
                },
            )
            assert evidence["status"] == "verified"
            draft["review_state"] = "candidate"
            draft["risk_flags"] = []
            saved = invoke(2, "save_event_drafts", {"drafts": [draft]})
            assert saved["ok"] is True, saved
        assert invoke(2, "finalize_event_drafts", {})["ok"] is True
        return AgentRunResult("review-search", 2, calls, 1, True, {"id": "review-search", "output": []})


def test_stage_c_agent_reads_b_workbench_and_materializes_events():
    session_factory = _db()
    run_id, item_ids = _seed_build(
        session_factory,
        rows=[("Acme Model 1 发布", 90, "active"), ("Acme SDK 更新", 82, "reserve")],
    )
    agent = _ToolAgent()

    result = run_event_cluster_job(session_factory=session_factory, run_id=run_id, ai_client=agent, reference_time=NOW)

    assert result.processed == 1
    assert result.events == 1
    assert result.candidate_event_ids == result.current_event_ids
    assert "content_text" not in agent.contexts[0]
    with session_factory() as session:
        events = session.scalars(select(IntelEvent).where(IntelEvent.build_id == run_id)).all()
        assert len(events) == 1
        assert events[0].primary_item_id == item_ids["Acme Model 1 发布"]
        agent_session = session.scalar(select(IntelAgentSession).where(IntelAgentSession.run_id == run_id))
        assert agent_session is not None
        assert agent_session.tool_call_count >= 3
        assert agent_session.finalization_requested is True


def test_stage_c_rejects_intent_only_event_but_keeps_it_in_the_audit_pool():
    session_factory = _db()
    run_id, _ = _seed_build(
        session_factory,
        rows=[("Acme 计划提升下一代模型竞争力", 90, "active")],
    )

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=_ToolAgent(intent_only=True),
        reference_time=NOW,
    )

    assert result.unresolved == 0
    assert len(result.current_event_ids) == 1
    assert result.candidate_event_ids == []
    with session_factory() as session:
        event = session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id))
        assert event is not None
        assert event.novelty_status == "new"
        assert event.review_state == "rejected"
        assert "intent_only_event" in json.loads(event.risk_flags_json)
        metadata = json.loads(event.resolution_raw_json)["draft_metadata"]
        assert metadata["substance_status"] == "intent_only"
        assert metadata["substantive_facts"] == []
        assert metadata["substance_guard"]["applied_review_state"] == "rejected"


def test_stage_c_does_not_send_confirmed_repeat_without_material_change_to_stage_d():
    session_factory = _db()
    run_id, _ = _seed_build(
        session_factory,
        rows=[("Acme Model 1 发布", 90, "active")],
    )
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.replace_published_daily_report(
            edition_date="2026-08-18",
            records=[
                {
                    "event_key": "url:https://source.example/1",
                    "title": "Acme Model 1 发布",
                    "url": "https://source.example/1",
                    "source_ids": ["agent-source"],
                    "source_refs": [
                        {
                            "source_id": "agent-source",
                            "source_url": "https://source.example/1",
                            "is_primary": True,
                        }
                    ],
                }
            ],
        )
        session.commit()

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=_ToolAgent(repeat=True),
        reference_time=NOW,
    )

    assert len(result.current_event_ids) == 1
    assert result.candidate_event_ids == []
    assert result.repeats == 1
    with session_factory() as session:
        event = session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id))
        assert event is not None
        assert event.novelty_status == "repeat"
        assert event.review_state == "rejected"
        assert "confirmed_repeat_without_material_change" in json.loads(event.risk_flags_json)


def test_stage_c_budget_exhaustion_becomes_needs_review_not_data_loss():
    session_factory = _db()
    run_id, _ = _seed_build(
        session_factory,
        rows=[("需要人工核查的事件", 90, "active"), ("另一个事件", 85, "active")],
    )

    result = run_event_cluster_job(session_factory=session_factory, run_id=run_id, ai_client=_BudgetAgent(), reference_time=NOW)

    assert result.unresolved == 2
    assert len(result.candidate_event_ids) == 2
    with session_factory() as session:
        states = session.scalars(select(IntelEvent.review_state).where(IntelEvent.build_id == run_id)).all()
        assert states == ["needs_review", "needs_review"]


def test_stage_c_binds_evidence_only_to_tavily_search_results():
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("有核验的发布", 90, "active")])

    run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=_ToolAgent(web_evidence=True),
        search_client=_SearchClient(),
        reference_time=NOW,
    )

    with session_factory() as session:
        evidence = session.scalars(select(IntelEventEvidence)).all()
        assert len(evidence) == 1
        assert evidence[0].status == "verified"
        assert evidence[0].event_id is not None


def test_stage_c_binds_evidence_to_the_exact_tavily_result_url():
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("打开页面后核验", 90, "active")])

    run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=_ToolAgent(web_evidence=True, opened_page_evidence=True),
        search_client=_SearchClient(url="https://source.example/opened"),
        reference_time=NOW,
    )

    with session_factory() as session:
        evidence = session.scalars(select(IntelEventEvidence)).all()
        assert len(evidence) == 1
        assert evidence[0].url == "https://source.example/opened"
        assert evidence[0].status == "verified"


def test_stage_c_rejects_unreturned_evidence_url_without_failing_aggregation():
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("无效核验地址", 90, "active")])

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=_ToolAgent(bad_evidence=True),
        search_client=_SearchClient(),
        reference_time=NOW,
    )

    assert result.events == 1
    with session_factory() as session:
        assert session.scalars(select(IntelEventEvidence)).all() == []


def test_stage_c_empty_tavily_results_do_not_create_unbound_evidence():
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("无来源列表的核验", 90, "active")])

    run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=_ToolAgent(web_evidence=True, source_less_web=True),
        search_client=_SearchClient(empty=True),
        reference_time=NOW,
    )

    with session_factory() as session:
        evidence = session.scalars(select(IntelEventEvidence)).all()
        assert evidence == []
        event = session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id))
        assert event is not None
        assert event.review_state == "candidate"


def test_stage_c_researches_needs_review_before_promoting_a_candidate():
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("需核验的发布", 90, "active")])
    agent = _ReviewFlowAgent()

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=agent,
        search_client=_SearchClient(),
        reference_time=NOW,
    )

    assert agent.first_finalize is not None
    assert "each needs_review draft requires its own Tavily verification pass before finalization" in agent.first_finalize["errors"]
    assert result.web_searches == 1
    with session_factory() as session:
        event = session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id))
        evidence = session.scalars(select(IntelEventEvidence)).all()
        assert event is not None
        assert event.review_state == "candidate"
        assert [row.status for row in evidence] == ["verified"]


def test_stage_c_keeps_needs_review_after_an_unresolved_research_pass():
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("无法核验的发布", 90, "active")])
    agent = _ReviewFlowAgent(unresolved=True)

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=agent,
        search_client=_SearchClient(empty=True),
        reference_time=NOW,
    )

    assert agent.first_finalize is not None
    assert result.unresolved == 1
    assert result.web_searches == 1
    with session_factory() as session:
        event = session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id))
        assert event is not None
        assert event.review_state == "needs_review"


def test_stage_c_batches_one_hundred_active_candidates_within_agent_tool_budget():
    session_factory = _db()
    run_id, _ = _seed_build(
        session_factory,
        rows=[(f"候选资讯 {index}", 90, "active") for index in range(100)],
    )

    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=_ToolAgent(),
        reference_time=NOW,
    )

    assert result.processed == 100
    assert result.events == 100
    # 4 pages + 13 batches + finalization; remains comfortably below the
    # configured 80-call ceiling and even the former 40-call budget.
    assert result.tool_calls == 18


def test_stage_c_renews_a_lease_sized_for_the_full_multi_turn_budget(monkeypatch):
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("租约续期事件", 90, "active")])
    agent = _ToolAgent()
    heartbeats: list[int] = []
    claims: list[int] = []
    original_heartbeat = IntelRepository.heartbeat_stage_task
    original_claim = IntelRepository.claim_stage_task

    def heartbeat(self, *args, **kwargs):
        heartbeats.append(int(kwargs["lease_seconds"]))
        return original_heartbeat(self, *args, **kwargs)

    def claim(self, *args, **kwargs):
        if kwargs.get("owner") == "stage-c-responses-agent":
            claims.append(int(kwargs["lease_seconds"]))
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(IntelRepository, "heartbeat_stage_task", heartbeat)
    monkeypatch.setattr(IntelRepository, "claim_stage_task", claim)
    result = run_event_cluster_job(
        session_factory=session_factory,
        run_id=run_id,
        ai_client=agent,
        reference_time=NOW,
        max_turns=24,
    )

    assert result.events == 1
    assert _stage_c_lease_seconds(agent, max_turns=24) == 840
    assert claims == [840]
    assert heartbeats and set(heartbeats) == {840}
    with session_factory() as session:
        stage = IntelRepository(session).get_stage(run_id, "cluster")
        assert stage is not None
        assert stage.metadata_dict["lease_seconds"] == 840


def test_stage_c_requires_agent_to_cover_all_active_candidates():
    session_factory = _db()
    run_id, _ = _seed_build(session_factory, rows=[("未覆盖候选", 90, "active")])

    with pytest.raises(AgentProtocolError, match="did not finalize"):
        run_event_cluster_job(
            session_factory=session_factory,
            run_id=run_id,
            ai_client=_InvalidCoverageAgent(),
            reference_time=NOW,
        )
    with session_factory() as session:
        repo = IntelRepository(session)
        stage = repo.get_stage(run_id, "cluster")
        task = repo.get_task(stage, subject_type="run", subject_id=run_id) if stage else None
        assert task is not None and task.status in {"failed", "retry_waiting"}
