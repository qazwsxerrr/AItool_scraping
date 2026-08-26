"""Stage C: stateful Responses agent for event-level aggregation.

Unlike the removed batch prompt, this job gives the model a bounded workbench
and durable tools. The model aggregates, researches material uncertainty, and
keeps unresolved claims auditable; deterministic code owns score admission,
coverage validation, persistence, and downstream contracts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.responses import AgentBudgetExceeded, AgentProtocolError, FunctionTool
from app.ai.skills.intel_triage import normalize_url
from app.ai.skills.stage_c_agent import StageCAgentClient
from app.ai.skills.stage_c_agent.prompts import (
    ATTACH_SEARCH_EVIDENCE_SCHEMA,
    FINALIZE_DRAFTS_SCHEMA,
    LIST_CANDIDATES_SCHEMA,
    LIST_DRAFTS_SCHEMA,
    MARK_UNRESOLVED_SCHEMA,
    READ_HISTORY_SCHEMA,
    READ_ITEMS_SCHEMA,
    SAVE_DRAFTS_SCHEMA,
    SEARCH_CANDIDATES_SCHEMA,
    SEARCH_WEB_SCHEMA,
    STAGE_C_AGENT_PROMPT_VERSION,
)
from app.ai.tavily import TavilySearchClient, TavilySearchError, TavilySearchResult
from app.config.limits import (
    DEFAULT_STAGE_C_AGENT_HISTORY_DAYS,
    DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS,
    DEFAULT_STAGE_C_AGENT_MAX_TURNS,
    DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES,
    STAGE_C_AGENT_VERSION,
)
from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import (
    DailyEditionReportEntry,
    IntelAgentSession,
    IntelCandidateAdmission,
    IntelEvent,
    IntelEventDraft,
    IntelEventEvidence,
    IntelItem,
    IntelRun,
    IntelRunStage,
    IntelRunStageTask,
)
from app.storage.repository import IntelRepository


DAILY_HISTORY_DAYS = DEFAULT_STAGE_C_AGENT_HISTORY_DAYS
STAGE_C_CANDIDATE_CONTRACT_VERSION = "stage_c_events_v6"
_TRACKING_QUERY_KEYS = {"ref", "source", "src", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid"}
_PRIMARY_POLICY_VERSION = "source_then_b1_priority_v2"
_VERIFICATION_POLICY_VERSION = "tavily_per_event_verification_v2"


class StageCDownstreamBusyError(RuntimeError):
    """A live Stage-D/export worker prevents safe Stage-C replacement."""


class StageCAgentContractError(RuntimeError):
    """The model tried to finalize an invalid event projection."""


class StageCLeaseLostError(RuntimeError):
    """The durable C-task lease expired before its agent state could be saved."""


@dataclass
class EventClusterResult:
    run_id: int
    processed: int = 0
    events: int = 0
    merged: int = 0
    repeats: int = 0
    updated: int = 0
    unresolved: int = 0
    turns: int = 0
    tool_calls: int = 0
    web_searches: int = 0
    event_ids: list[int] = field(default_factory=list)
    current_event_ids: list[int] = field(default_factory=list)
    candidate_event_ids: list[int] = field(default_factory=list)
    reference_time: datetime | None = None
    input_audit: dict[str, Any] = field(default_factory=dict)


def normalize_event_title(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_event_url(value: Any) -> str | None:
    if value is None:
        return None
    try:
        raw = normalize_url(value) or str(value).strip()
    except Exception:
        raw = str(value).strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.casefold().rstrip("/") or None
    if not parts.scheme or not parts.netloc:
        return raw.casefold().rstrip("/") or None
    scheme = parts.scheme.casefold()
    host = (parts.hostname or parts.netloc).casefold()
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    query_items = [
        (key, query_value)
        for key, query_value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    query_items.sort(key=lambda pair: (pair[0], pair[1]))
    return urlunsplit((scheme, netloc, path, urlencode(query_items, doseq=True), ""))


def exact_identity_keys(value: Any) -> tuple[str, ...]:
    values = _mapping(value)
    url = canonical_event_url(values.get("canonical_url") or values.get("url") or values.get("source_url"))
    external_id = _normalize_external_id(values.get("external_id") or values.get("guid"))
    values = [value for value in (f"url:{url}" if url else None, f"external:{external_id}" if external_id else None) if value]
    return tuple(dict.fromkeys(values))


def canonical_event_key(value: Any) -> str:
    keys = exact_identity_keys(value)
    if keys:
        return keys[0]
    values = _mapping(value)
    for name in ("id", "item_id", "primary_item_id"):
        try:
            item_id = int(values.get(name))
        except (TypeError, ValueError, OverflowError):
            continue
        if item_id > 0:
            return f"item:{item_id}"
    title = normalize_event_title(values.get("title") or values.get("original_title"))
    return f"title:{title}" if title else "item:unknown"


def run_event_cluster_job(
    *,
    session_factory: sessionmaker[Session],
    run_id: int,
    ai_client: Any,
    force: bool = False,
    now: datetime | None = None,
    reference_time: datetime | None = None,
    max_turns: int = DEFAULT_STAGE_C_AGENT_MAX_TURNS,
    max_tool_calls: int = DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS,
    max_web_searches: int = DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES,
    search_client: TavilySearchClient | None = None,
) -> EventClusterResult:
    """Run C as an auditable multi-turn tool session for one daily build."""

    result = EventClusterResult(run_id=int(run_id))
    owner = "stage-c-responses-agent"
    max_turns = _positive_int(max_turns, DEFAULT_STAGE_C_AGENT_MAX_TURNS)
    max_tool_calls = _positive_int(max_tool_calls, DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS)
    max_web_searches = _nonnegative_int(max_web_searches, DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES)
    model_name = str(getattr(ai_client, "model", None) or "responses-agent")
    lease_seconds = _stage_c_lease_seconds(ai_client, max_turns=max_turns)

    with session_factory() as session:
        repo = IntelRepository(session)
        run = session.get(IntelRun, int(run_id))
        if run is None or run.edition_id is None:
            raise ValueError("Stage C requires the current daily edition build")
        _assert_downstream_idle(repo, int(run_id))
        current = _as_utc(reference_time) or _as_utc(run.reference_time) or _as_utc(now) or datetime.now(timezone.utc)
        result.reference_time = current
        admissions = _load_admissions(session, repo=repo, run_id=int(run_id))
        history = _load_published_daily_history(repo, run=run, days=DAILY_HISTORY_DAYS)
        result.processed = len(admissions["active"])
        result.input_audit = {
            "active": len(admissions["active"]),
            "reserve": len(admissions["reserve"]),
            "history": len(history),
            "admission_policy_fingerprints": sorted(
                {row.policy_fingerprint for row in [*admissions["active"], *admissions["reserve"]] if row.policy_fingerprint}
            ),
        }
        input_fingerprint = _cluster_input_fingerprint(admissions, history)
        config_fingerprint = _stage_c_config_fingerprint(
            model=model_name,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            max_web_searches=max_web_searches,
            lease_seconds=lease_seconds,
            search_provider="tavily" if search_client is not None and search_client.is_configured else "disabled",
        )
        stage = repo.ensure_stage(
            int(run_id),
            "cluster",
            config_fingerprint=config_fingerprint,
            reference_time=current,
            metadata={
                "aggregation_mode": "responses_agent_tools_v4",
                "agent_version": STAGE_C_AGENT_VERSION,
                "prompt_version": STAGE_C_AGENT_PROMPT_VERSION,
                "candidate_contract_version": STAGE_C_CANDIDATE_CONTRACT_VERSION,
                "verification_policy": _VERIFICATION_POLICY_VERSION,
                "history_days": DAILY_HISTORY_DAYS,
                "max_turns": max_turns,
                "max_tool_calls": max_tool_calls,
                "max_web_searches": max_web_searches,
                "lease_seconds": lease_seconds,
                "search_provider": "tavily" if search_client is not None and search_client.is_configured else "disabled",
                "history_scope": "previous_three_published_editions",
            },
        )
        task = repo.ensure_stage_task(
            stage,
            subject_type="run",
            subject_id=int(run_id),
            target_run_id=int(run_id),
            input_fingerprint=input_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        claimed = repo.claim_stage_task(
            stage,
            task_id=task.id,
            owner=owner,
            force=force,
            lease_seconds=lease_seconds,
            input_fingerprint=input_fingerprint,
            config_fingerprint=config_fingerprint,
        )
        if claimed is None:
            if repo.task_is_reusable(task, input_fingerprint=input_fingerprint, config_fingerprint=config_fingerprint):
                return _result_from_task(result, task)
            raise RuntimeError("Stage C task is already running")
        task = claimed
        try:
            repo.invalidate_downstream_stages(
                int(run_id),
                stage_names=("stage_d", "export"),
                upstream_stage="cluster",
            )
        except RuntimeError as exc:
            session.rollback()
            if str(exc).startswith("downstream_stage_busy:"):
                raise StageCDownstreamBusyError(str(exc)) from exc
            raise
        session.commit()

        try:
            if not admissions["active"]:
                _clear_build_events(session, run_id=int(run_id))
                completed = repo.complete_stage_task(
                    task,
                    owner=owner,
                    result={
                        "schema_version": STAGE_C_CANDIDATE_CONTRACT_VERSION,
                        "event_ids": [],
                        "current_event_ids": [],
                        "candidate_event_ids": [],
                        "processed": 0,
                        "input_audit": result.input_audit,
                    },
                    metadata={"input_audit": result.input_audit},
                )
                if completed is None:
                    raise StageCLeaseLostError("Stage C task lease was lost before empty-result completion")
                session.commit()
                return result
            if ai_client is None or not callable(getattr(ai_client, "run", None)):
                raise TypeError("Stage C requires a Responses agent client with run()")

            existing_session = repo.get_agent_session(int(run_id), stage_name="cluster")
            reset_agent = bool(
                force
                or existing_session is None
                or existing_session.state.get("input_fingerprint") != input_fingerprint
                or existing_session.prompt_version != STAGE_C_AGENT_PROMPT_VERSION
            )
            agent_session = repo.start_agent_session(
                run_id=int(run_id),
                stage_id=int(stage.id),
                stage_name="cluster",
                model=model_name,
                prompt_version=STAGE_C_AGENT_PROMPT_VERSION,
                max_turns=max_turns,
                max_tool_calls=max_tool_calls,
                max_web_searches=max_web_searches,
                state={
                    "input_fingerprint": input_fingerprint,
                    "config_fingerprint": config_fingerprint,
                    "search_provider": "tavily" if search_client is not None and search_client.is_configured else "disabled",
                    "reference_time": current.isoformat(),
                    "lease_seconds": lease_seconds,
                },
                reset=reset_agent,
            )
            agent_session.status = "running"
            agent_session.started_at = agent_session.started_at or datetime.now(timezone.utc)
            agent_session.error_code = None
            agent_session.error_message = None
            session.commit()

            tools = _StageCAgentTools(
                session=session,
                repo=repo,
                run=run,
                agent_session=agent_session,
                admissions=admissions,
                history=history,
                search_client=search_client,
                max_web_searches=max_web_searches,
            )

            def on_response(turn: int, response: Mapping[str, Any]) -> None:
                if repo.heartbeat_stage_task(task, owner=owner, lease_seconds=lease_seconds) is None:
                    raise StageCLeaseLostError("Stage C task lease was lost while saving an agent response")
                repo.append_agent_step(
                    int(agent_session.id),
                    turn=turn,
                    kind="response",
                    raw_response=response,
                )
                agent_session.response_id = _text(response.get("id")) or agent_session.response_id
                agent_session.turn_count = max(agent_session.turn_count, int(turn))
                session.commit()

            def on_tool(turn: int, call: Mapping[str, Any], output: Mapping[str, Any]) -> None:
                if repo.heartbeat_stage_task(task, owner=owner, lease_seconds=lease_seconds) is None:
                    raise StageCLeaseLostError("Stage C task lease was lost while saving an agent tool call")
                repo.append_agent_step(
                    int(agent_session.id),
                    turn=turn,
                    kind="tool_call",
                    tool_name=_text(call.get("name")),
                    call_id=_text(call.get("call_id")),
                    input_value=_parse_call_arguments(call.get("arguments")),
                    output_value=output,
                    status="success" if output.get("ok", True) else "error",
                    error_message=_text(output.get("error")),
                )
                agent_session.tool_call_count += 1
                if str(call.get("name") or "") == "search_web":
                    agent_session.web_search_count += 1
                session.commit()

            if not agent_session.finalization_requested:
                agent_result = ai_client.run(
                    initial_context={
                        "run_id": int(run_id),
                        "reference_time": current.isoformat(),
                        "active_candidate_count": len(admissions["active"]),
                        "reserve_candidate_count": len(admissions["reserve"]),
                        "history_window_days": DAILY_HISTORY_DAYS,
                        "instructions": (
                            "Use local tools to aggregate every active candidate. Keep follow-up events separate, compare only "
                            "the previous three published editions for novelty, and use search_web for material uncertainty "
                            "before retaining an event as needs_review. Finalize after all active candidates are covered."
                        ),
                    },
                    function_tools=tools.function_tools,
                    max_turns=max_turns,
                    max_tool_calls=max_tool_calls,
                    # A retry starts a fresh model turn but reads persisted
                    # drafts/tools; this avoids replaying an interrupted call.
                    previous_response_id=None,
                    on_response=on_response,
                    on_tool=on_tool,
                )
                result.turns = agent_result.turns
                result.tool_calls = agent_result.tool_calls
                result.web_searches = int(agent_session.web_search_count)
            if repo.heartbeat_stage_task(task, owner=owner, lease_seconds=lease_seconds) is None:
                raise StageCLeaseLostError("Stage C task lease was lost before event materialization")
            if not agent_session.finalization_requested:
                raise AgentProtocolError("C agent did not finalize its event drafts")

            _materialize_agent_events(
                session=session,
                repo=repo,
                run_id=int(run_id),
                current=current,
                agent_session=agent_session,
                admissions=admissions,
                result=result,
            )
            agent_session.status = "succeeded"
            agent_session.finished_at = datetime.now(timezone.utc)
            completed = repo.complete_stage_task(
                task,
                owner=owner,
                result=_task_result(result, agent_session),
                raw_response={"agent_session_id": int(agent_session.id), "response_id": agent_session.response_id},
                metadata={
                    "input_audit": result.input_audit,
                    "agent_session_id": int(agent_session.id),
                    "turns": result.turns or agent_session.turn_count,
                    "tool_calls": result.tool_calls or agent_session.tool_call_count,
                    "web_searches": result.web_searches or agent_session.web_search_count,
                },
            )
            if completed is None:
                raise StageCLeaseLostError("Stage C task lease was lost before completion")
            session.commit()
            return result
        except AgentBudgetExceeded as exc:
            # Budget exhaustion is uncertainty, not a reason to lose a
            # qualified source. Close the uncovered active set into explicit
            # needs-review singleton drafts, then commit a valid projection.
            _ensure_unresolved_drafts(
                repo=repo,
                agent_session=agent_session,
                admissions=admissions,
                reason="agent_budget_exhausted",
            )
            _materialize_agent_events(
                session=session,
                repo=repo,
                run_id=int(run_id),
                current=current,
                agent_session=agent_session,
                admissions=admissions,
                result=result,
            )
            agent_session.status = "succeeded"
            agent_session.finished_at = datetime.now(timezone.utc)
            agent_session.error_code = "agent_budget_exhausted"
            agent_session.error_message = str(exc)
            result.input_audit["budget_exhausted"] = True
            completed = repo.complete_stage_task(
                task,
                owner=owner,
                result=_task_result(result, agent_session),
                raw_response={"agent_session_id": int(agent_session.id), "error": str(exc)},
                metadata={"input_audit": result.input_audit, "agent_session_id": int(agent_session.id)},
            )
            if completed is None:
                raise StageCLeaseLostError("Stage C task lease was lost before budget-fallback completion")
            session.commit()
            return result
        except Exception as exc:
            session.rollback()
            _fail_agent_run(
                session_factory=session_factory,
                run_id=int(run_id),
                owner=owner,
                error=exc,
            )
            raise


def run_event_cluster_from_settings(
    *,
    settings: Settings,
    run_id: int,
    ai_client: Any | None = None,
    force: bool = False,
    now: datetime | None = None,
    reference_time: datetime | None = None,
) -> EventClusterResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    return run_event_cluster_job(
        session_factory=create_session_factory(engine),
        ai_client=ai_client or StageCAgentClient.from_settings(settings),
        run_id=run_id,
        force=force,
        now=now,
        reference_time=reference_time,
        max_turns=settings.stage_c_agent_max_turns,
        max_tool_calls=settings.stage_c_agent_max_tool_calls,
        max_web_searches=settings.stage_c_agent_max_web_searches,
        search_client=TavilySearchClient(
            api_key=settings.tavily_api_key,
            api_url=settings.tavily_api_url,
            timeout_seconds=settings.tavily_timeout_seconds,
            max_retries=settings.request_retries,
        ),
    )


class _StageCAgentTools:
    def __init__(
        self,
        *,
        session: Session,
        repo: IntelRepository,
        run: IntelRun,
        agent_session: IntelAgentSession,
        admissions: Mapping[str, Sequence[IntelCandidateAdmission]],
        history: Sequence[Mapping[str, Any]],
        search_client: TavilySearchClient | None,
        max_web_searches: int,
    ) -> None:
        self.session = session
        self.repo = repo
        self.run = run
        self.agent_session = agent_session
        self.history = list(history)
        self.history_by_key = {
            str(row.get("event_key")): dict(row)
            for row in self.history
            if str(row.get("event_key") or "").strip()
        }
        self.history_identity_index = _history_identity_index(self.history)
        self.search_client = search_client
        self.max_web_searches = max_web_searches
        self.search_results: dict[str, TavilySearchResult] = {}
        self.search_attempted_draft_keys: set[str] = set()
        self.search_calls = 0
        self.admissions = {key: list(value) for key, value in admissions.items()}
        self.by_item_id = {
            int(row.item_id): row
            for rows in self.admissions.values()
            for row in rows
        }
        self.active_ids = {int(row.item_id) for row in self.admissions.get("active", ())}
        self._restore_search_state()

    @property
    def function_tools(self) -> list[FunctionTool]:
        return [
            FunctionTool("list_candidates", "分页列出 B 已准入的 active 或 reserve 候选概要。", LIST_CANDIDATES_SCHEMA, self.list_candidates),
            FunctionTool("list_event_drafts", "列出当前会话已持久化的事件草稿，供失败恢复时继续。", LIST_DRAFTS_SCHEMA, self.list_event_drafts),
            FunctionTool("read_items", "读取候选的完整原文、B 分析和来源元数据。", READ_ITEMS_SCHEMA, self.read_items),
            FunctionTool("search_candidates", "在 B 准入候选内按词检索相关内容。", SEARCH_CANDIDATES_SCHEMA, self.search_candidates),
            FunctionTool("read_recent_history", "查询过去三天的已发布日报事件，判断重复或更新。", READ_HISTORY_SCHEMA, self.read_recent_history),
            FunctionTool("save_event_drafts", "批量保存或更新 1–8 个事件聚合草稿。", SAVE_DRAFTS_SCHEMA, self.save_event_drafts),
            FunctionTool("search_web", "通过 Tavily 搜索公开网页并返回可审计的来源结果。", SEARCH_WEB_SCHEMA, self.search_web),
            FunctionTool("attach_search_evidence", "把 Tavily 结果绑定到草稿和具体核验 claim。", ATTACH_SEARCH_EVIDENCE_SCHEMA, self.attach_search_evidence),
            FunctionTool("mark_unresolved", "把无法可靠聚合的 active 候选显式放入待审事件。", MARK_UNRESOLVED_SCHEMA, self.mark_unresolved),
            FunctionTool("finalize_event_drafts", "检查 active 覆盖并提交事件草稿。", FINALIZE_DRAFTS_SCHEMA, self.finalize_event_drafts),
        ]

    def list_candidates(self, args: dict[str, Any]) -> Mapping[str, Any]:
        bucket = str(args.get("bucket") or "active")
        if bucket not in {"active", "reserve"}:
            raise ValueError("bucket must be active or reserve")
        offset = max(0, int(args.get("offset") or 0))
        limit = max(1, min(30, int(args.get("limit") or 20)))
        rows = self.admissions[bucket]
        return {
            "ok": True,
            "bucket": bucket,
            "total": len(rows),
            "offset": offset,
            "items": [_compact_admission(row) for row in rows[offset : offset + limit]],
        }

    def list_event_drafts(self, args: dict[str, Any]) -> Mapping[str, Any]:
        del args
        drafts = self.repo.list_agent_drafts(int(self.agent_session.id))
        return {
            "ok": True,
            "drafts": [
                _draft_tool_view(draft)
                for draft in drafts
            ],
        }

    def read_items(self, args: dict[str, Any]) -> Mapping[str, Any]:
        ids = _unique_positive_ids(args.get("item_ids"), limit=10)
        unknown = [item_id for item_id in ids if item_id not in self.by_item_id]
        if unknown:
            return {"ok": False, "error": f"item ids are outside the C workbench: {unknown}"}
        return {"ok": True, "items": [_full_admission(self.by_item_id[item_id]) for item_id in ids]}

    def search_candidates(self, args: dict[str, Any]) -> Mapping[str, Any]:
        query = str(args.get("query") or "").strip().casefold()
        bucket = str(args.get("bucket") or "all")
        limit = max(1, min(30, int(args.get("limit") or 20)))
        if not query:
            return {"ok": False, "error": "query is required"}
        if bucket not in {"active", "reserve", "all"}:
            return {"ok": False, "error": "bucket must be active, reserve, or all"}
        tokens = [token for token in re.split(r"\s+", query) if token]
        rows = self.admissions["active"] + self.admissions["reserve"] if bucket == "all" else self.admissions[bucket]
        matched = [row for row in rows if _candidate_matches(row.item, tokens)]
        return {"ok": True, "query": query, "total": len(matched), "items": [_compact_admission(row) for row in matched[:limit]]}

    def read_recent_history(self, args: dict[str, Any]) -> Mapping[str, Any]:
        query = str(args.get("query") or "").strip().casefold()
        limit = max(1, min(20, int(args.get("limit") or 10)))
        tokens = [token for token in re.split(r"\s+", query) if token]
        rows = [row for row in self.history if _history_matches(row, tokens)] if tokens else self.history
        return {"ok": True, "total": len(rows), "events": rows[:limit]}

    def save_event_drafts(self, args: dict[str, Any]) -> Mapping[str, Any]:
        raw_drafts = args.get("drafts")
        if not isinstance(raw_drafts, list) or not 1 <= len(raw_drafts) <= 8:
            return {"ok": False, "error": "drafts must contain between 1 and 8 objects"}
        if not all(isinstance(value, Mapping) for value in raw_drafts):
            return {"ok": False, "error": "every draft must be an object"}

        prepared: list[tuple[dict[str, Any], list[int], dict[str, Any], dict[str, Any]]] = []
        assigned: dict[int, str] = {}
        keys: set[str] = set()
        for raw_value in raw_drafts:
            draft_args = dict(raw_value)
            draft_key = str(draft_args.get("draft_key") or "").strip()
            if not draft_key:
                return {"ok": False, "error": "draft_key is required"}
            if draft_key in keys:
                return {"ok": False, "error": f"duplicate draft_key in batch: {draft_key}"}
            keys.add(draft_key)
            ids = _unique_positive_ids(draft_args.get("item_ids"), limit=40)
            if not ids:
                return {"ok": False, "error": f"item_ids are required for draft: {draft_key}"}
            unknown = [item_id for item_id in ids if item_id not in self.by_item_id]
            if unknown:
                return {"ok": False, "error": f"item ids are outside the C workbench: {unknown}"}
            for item_id in ids:
                prior_key = assigned.get(item_id)
                if prior_key is not None:
                    return {"ok": False, "error": f"item {item_id} appears in both {prior_key} and {draft_key}"}
                assigned[item_id] = draft_key
            try:
                history = _prepare_draft_history(
                    draft_args,
                    item_ids=ids,
                    admissions=self.by_item_id,
                    history_by_key=self.history_by_key,
                    history_identity_index=self.history_identity_index,
                )
                publishability = _prepare_draft_publishability(
                    draft_args,
                    item_ids=ids,
                    novelty_status=str(history["novelty_status"]),
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            prepared.append((draft_args, ids, history, publishability))

        saved: list[dict[str, Any]] = []
        try:
            with self.session.begin_nested():
                for draft_args, ids, history, publishability in prepared:
                    caveats = _strings(draft_args.get("caveats"))
                    caveats.extend(value for value in history["risk_flags"] if value not in caveats)
                    caveats.extend(value for value in publishability["risk_flags"] if value not in caveats)
                    metadata = {
                        "saved_by": "responses_agent_batch",
                        "batch_size": len(prepared),
                        "event_family_key": _event_family_key(draft_args),
                        "facts": publishability["facts"],
                        "history_status": history["history_status"],
                        "history_guard": history["guard"],
                        "publishability": publishability["publishability"],
                        "publishability_guard": publishability["guard"],
                        "split_reason": _text(draft_args.get("split_reason")),
                        "caveats": caveats,
                    }
                    draft = self.repo.upsert_agent_draft(
                        int(self.agent_session.id),
                        draft_key=str(draft_args.get("draft_key") or ""),
                        item_ids=ids,
                        title=str(draft_args.get("title") or ""),
                        summary_cn=_text(draft_args.get("summary_cn")),
                        topic=str(draft_args.get("topic") or "technology_insight"),
                        keywords=_strings(draft_args.get("keywords")),
                        entities=[
                            dict(value)
                            for value in draft_args.get("entities", [])
                            if isinstance(value, Mapping)
                        ],
                        novelty_status=str(history["novelty_status"]),
                        prior_event_key=_text(history.get("prior_event_key")),
                        review_state=str(publishability["review_state"]),
                        risk_flags=caveats,
                        metadata=metadata,
                        allow_member_reassignment=True,
                    )
                    saved.append(
                        {
                            "draft_key": draft.draft_key,
                            "draft_id": int(draft.id),
                            "item_ids": ids,
                            "review_state": draft.review_state,
                            "publishability": publishability["publishability"],
                            "event_family_key": metadata["event_family_key"],
                        }
                    )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self.session.commit()
        return {"ok": True, "drafts": saved}

    def search_web(self, args: dict[str, Any]) -> Mapping[str, Any]:
        draft_key = _text(args.get("draft_key"))
        if draft_key:
            self.search_attempted_draft_keys.add(draft_key)
        if self.search_calls >= self.max_web_searches:
            return {"ok": False, "error": "Stage C Tavily search budget exhausted", "error_code": "search_budget_exhausted"}
        if self.search_client is None or not self.search_client.is_configured:
            return {"ok": False, "error": "Tavily API is not configured", "error_code": "search_not_configured"}
        self.search_calls += 1
        try:
            response = self.search_client.search(
                str(args.get("query") or ""),
                topic=str(args.get("topic") or "general"),
                max_results=int(args.get("max_results") or 5),
            )
        except TavilySearchError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "error_code": "tavily_search_failed",
                "status_code": exc.status_code,
                "retryable": exc.retryable,
            }
        for row in response.results:
            self.search_results[row.result_id] = row
        return {
            "ok": True,
            "provider": "tavily",
            "draft_key": draft_key,
            "claim": _text(args.get("claim")),
            **response.as_dict(),
        }

    def attach_search_evidence(self, args: dict[str, Any]) -> Mapping[str, Any]:
        result_id = str(args.get("result_id") or "").strip()
        row = self.search_results.get(result_id)
        if row is None:
            return {"ok": False, "error": "result_id was not returned by a Tavily search in this session"}
        verdict = str(args.get("verdict") or "contextual")
        status = {"supports": "verified", "contradicts": "contradicted", "contextual": "recorded"}.get(verdict)
        if status is None:
            return {"ok": False, "error": "invalid evidence verdict"}
        try:
            evidence = self.repo.record_agent_evidence(
                int(self.agent_session.id),
                draft_key=str(args.get("draft_key") or ""),
                url=row.url,
                final_url=row.url,
                title=row.title,
                excerpt=row.content,
                verification_claim=_text(args.get("claim")),
                source_scope="tavily",
                status=status,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self.session.commit()
        return {
            "ok": True,
            "evidence_id": int(evidence.id),
            "result_id": result_id,
            "host": evidence.host,
            "status": status,
        }

    def _restore_search_state(self) -> None:
        for step in self.agent_session.steps:
            if step.kind != "tool_call" or step.tool_name != "search_web":
                continue
            self.search_calls += 1
            input_value = _json_mapping(step.input_json)
            output_value = _json_mapping(step.output_json)
            draft_key = _text(input_value.get("draft_key") or output_value.get("draft_key"))
            if draft_key:
                self.search_attempted_draft_keys.add(draft_key)
            for raw in output_value.get("results") or ():
                if not isinstance(raw, Mapping):
                    continue
                result_id = _text(raw.get("result_id"))
                url = _text(raw.get("url"))
                if not result_id or not url:
                    continue
                self.search_results[result_id] = TavilySearchResult(
                    result_id=result_id,
                    title=_text(raw.get("title")),
                    url=url,
                    content=_text(raw.get("content")),
                    score=_float_or_none(raw.get("score")),
                    published_date=_text(raw.get("published_date")),
                )

    def mark_unresolved(self, args: dict[str, Any]) -> Mapping[str, Any]:
        ids = _unique_positive_ids(args.get("item_ids"), limit=40)
        unknown = [item_id for item_id in ids if item_id not in self.active_ids]
        if unknown:
            return {"ok": False, "error": f"unresolved items must be active candidates: {unknown}"}
        return _save_unresolved_draft(self.repo, self.agent_session, self.by_item_id, ids, str(args.get("reason") or "needs_review"))

    def finalize_event_drafts(self, args: dict[str, Any]) -> Mapping[str, Any]:
        del args
        validation = _validate_agent_drafts(
            self.repo,
            self.agent_session,
            self.active_ids,
            admissions=self.admissions,
        )
        review_drafts = [
            draft
            for draft in self.repo.list_agent_drafts(int(self.agent_session.id))
            if str(draft.review_state or "").casefold() == "needs_review"
        ]
        verification_pending = [
            draft
            for draft in review_drafts
            if self.max_web_searches > 0
            and self.search_client is not None
            and self.search_client.is_configured
            and draft.draft_key not in self.search_attempted_draft_keys
        ]
        errors = list(validation["errors"])
        if verification_pending:
            errors.append("each needs_review draft requires its own Tavily verification pass before finalization")
        if errors:
            return {
                "ok": False,
                "errors": errors,
                "missing_active_ids": validation["missing_active_ids"],
                "verification_pending": [_pending_verification_view(draft) for draft in verification_pending]
                if verification_pending
                else [],
                "next_action": (
                    "Use search_web for each unresolved draft, attach returned sources to its claims, and revise the draft. "
                    "Keep publishability=needs_review only when the research pass still cannot resolve it."
                    if verification_pending
                    else None
                ),
            }
        self.agent_session.finalization_requested = True
        self.agent_session.status = "finalizing"
        self.session.commit()
        return {"ok": True, "draft_count": validation["draft_count"], "_finalized": True}


def _load_admissions(
    session: Session,
    *,
    repo: IntelRepository,
    run_id: int,
) -> dict[str, list[IntelCandidateAdmission]]:
    stage = repo.get_stage(int(run_id), "analyze")
    if stage is None:
        raise ValueError("Stage C requires the Stage B analysis stage")
    item_tasks = repo.list_stage_tasks(stage, subject_type="item", include_expired=True)
    if any(task.status in {"pending", "running", "retry_waiting"} for task in item_tasks):
        raise ValueError("Stage C requires Stage B item tasks to finish before aggregation")
    rows = repo.list_candidate_admissions(int(run_id), decisions=("active", "reserve"))
    if item_tasks and not rows:
        raise ValueError("Stage C requires the completed Stage B admission projection")
    active = [row for row in rows if row.decision == "active"]
    reserve = [row for row in rows if row.decision == "reserve"]
    return {"active": active, "reserve": reserve}


def _load_published_daily_history(
    repo: IntelRepository,
    *,
    run: IntelRun,
    days: int,
) -> list[dict[str, Any]]:
    if run.edition is None:
        return []
    entries = repo.list_prior_daily_report_entries(edition_date=run.edition.edition_date, days=days)
    result: list[dict[str, Any]] = []
    for entry in entries:
        result.append(
            {
                "event_key": entry.event_key,
                "edition_date": entry.edition.edition_date.isoformat() if entry.edition is not None else None,
                "title": entry.title,
                "summary": entry.summary,
                "topic": entry.topic,
                "keywords": entry.keywords,
                "entities": entry.entities,
                "risk_flags": entry.risk_flags,
                "source_refs": entry.source_refs,
                "verification_refs": entry.verification_refs,
            }
        )
    return result


def _draft_tool_view(draft: IntelEventDraft) -> dict[str, Any]:
    metadata = _json_mapping(draft.metadata_json)
    return {
        "draft_key": draft.draft_key,
        "title": draft.title,
        "item_ids": [int(member.item_id) for member in draft.members],
        "event_family_key": metadata.get("event_family_key"),
        "history_status": metadata.get("history_status") or draft.novelty_status,
        "publishability": metadata.get("publishability") or draft.review_state,
        "caveats": metadata.get("caveats") or _json_strings(draft.risk_flags_json),
        "metadata": metadata,
    }


def _pending_verification_view(draft: IntelEventDraft) -> dict[str, Any]:
    view = _draft_tool_view(draft)
    return {
        "draft_key": view["draft_key"],
        "title": view["title"],
        "event_family_key": view.get("event_family_key"),
        "caveats": view.get("caveats") or [],
    }


def _materialize_agent_events(
    *,
    session: Session,
    repo: IntelRepository,
    run_id: int,
    current: datetime,
    agent_session: IntelAgentSession,
    admissions: Mapping[str, Sequence[IntelCandidateAdmission]],
    result: EventClusterResult,
) -> None:
    active_ids = {int(row.item_id) for row in admissions["active"]}
    validation = _validate_agent_drafts(repo, agent_session, active_ids, admissions=admissions)
    if validation["errors"]:
        raise StageCAgentContractError("; ".join(validation["errors"]))
    all_admissions = {int(row.item_id): row for rows in admissions.values() for row in rows}
    _clear_build_events(session, run_id=run_id)
    result.event_ids.clear()
    result.current_event_ids.clear()
    result.candidate_event_ids.clear()
    seen_keys: set[str] = set()
    assigned_ids = {
        int(member.item_id)
        for draft in repo.list_agent_drafts(int(agent_session.id))
        for member in draft.members
    }
    for draft in repo.list_agent_drafts(int(agent_session.id)):
        member_ids = [int(member.item_id) for member in draft.members]
        draft_identities = {
            key
            for item_id in member_ids
            for key in exact_identity_keys(_item_mapping(all_admissions[item_id].item))
        }
        for reserve in admissions.get("reserve", ()):
            reserve_id = int(reserve.item_id)
            if reserve_id in assigned_ids:
                continue
            if draft_identities & set(exact_identity_keys(_item_mapping(reserve.item))):
                member_ids.append(reserve_id)
                assigned_ids.add(reserve_id)
        members = [all_admissions[item_id].item for item_id in member_ids]
        primary = _select_primary_item(members)
        event_key = canonical_event_key(_item_mapping(primary))
        if event_key in seen_keys:
            event_key = f"agent:{int(agent_session.id)}:{int(draft.id)}"
        seen_keys.add(event_key)
        source_ids = _unique_strings(item.source_id for item in members)
        source_groups = _unique_strings(getattr(item.source, "source_group", None) for item in members)
        review_topics = [item.ai_review.topic for item in members if item.ai_review is not None and item.ai_review.topic]
        review_keywords = [keyword for item in members if item.ai_review is not None for keyword in item.ai_review.keywords]
        review_entities = [entity for item in members if item.ai_review is not None for entity in item.ai_review.entities]
        risk_flags = _json_strings(draft.risk_flags_json)
        if draft.review_state == "needs_review" and "needs_review" not in risk_flags:
            risk_flags.append("needs_review")
        event = repo.upsert_event(
            run_id=run_id,
            event_key=event_key,
            canonical_url=primary.canonical_url or primary.source_url,
            external_id=primary.external_id,
            normalized_title=normalize_event_title(draft.title),
            title=draft.title,
            summary_cn=draft.summary_cn,
            topic=draft.topic or (review_topics[0] if review_topics else "technology_insight"),
            keywords=_unique_strings([*_json_strings(draft.keywords_json), *review_keywords]),
            entities=_unique_json_objects([*_json_objects(draft.entities_json), *review_entities]),
            content_class=primary.content_class,
            source_group=getattr(primary.source, "source_group", None),
            source_ids=source_ids,
            source_groups=source_groups,
            identity_keys=[key for item in members for key in exact_identity_keys(_item_mapping(item))],
            display_score=max((int(item.b1_priority or 0) for item in members), default=0),
            novelty_status=draft.novelty_status,
            state="candidate",
            review_state=draft.review_state,
            resolution_method="responses_agent",
            resolution_raw={
                "agent_session_id": int(agent_session.id),
                "draft_key": draft.draft_key,
                "prompt_version": agent_session.prompt_version,
                "prior_event_key": draft.prior_event_key,
                "draft_metadata": _json_mapping(draft.metadata_json),
            },
            risk_flags=risk_flags,
            primary_item_id=int(primary.id),
            first_seen_at=min((_as_utc(item.published_at or item.captured_at) for item in members), default=current),
            last_seen_at=max((_as_utc(item.published_at or item.captured_at) for item in members), default=current),
        )
        event.primary_item_id = int(primary.id)
        for item in members:
            relation = "primary" if int(item.id) == int(primary.id) else _member_relation(item, primary)
            repo.upsert_event_item(
                int(event.id),
                int(item.id),
                source_id=item.source_id,
                source_group=getattr(item.source, "source_group", None),
                identity_key=next(iter(exact_identity_keys(_item_mapping(item))), None),
                match_type=relation,
                is_primary=relation == "primary",
                lineage={"agent_session_id": int(agent_session.id), "draft_key": draft.draft_key, "relation": relation},
            )
        for evidence in session.scalars(
            select(IntelEventEvidence).where(
                IntelEventEvidence.session_id == int(agent_session.id),
                IntelEventEvidence.draft_id == int(draft.id),
            )
        ).all():
            evidence.event_id = int(event.id)
        result.current_event_ids.append(int(event.id))
        result.merged += max(0, len(members) - 1)
        novelty = str(draft.novelty_status or "uncertain")
        if novelty == "repeat":
            result.repeats += 1
        else:
            if novelty == "updated":
                result.updated += 1
            else:
                result.events += 1
                result.event_ids.append(int(event.id))
        # Keep every materialized event in the C audit pool, but only pass the
        # two reviewable states downstream. Rejected rows remain traceable in C.
        if str(draft.review_state or "").casefold() in {"candidate", "needs_review"}:
            result.candidate_event_ids.append(int(event.id))
        if draft.review_state == "needs_review":
            result.unresolved += 1
    session.flush()


def _ensure_unresolved_drafts(
    *,
    repo: IntelRepository,
    agent_session: IntelAgentSession,
    admissions: Mapping[str, Sequence[IntelCandidateAdmission]],
    reason: str,
) -> None:
    all_rows = {int(row.item_id): row for rows in admissions.values() for row in rows}
    active_ids = {int(row.item_id) for row in admissions["active"]}
    validation = _validate_agent_drafts(repo, agent_session, active_ids)
    for item_id in validation["missing_active_ids"]:
        _save_unresolved_draft(repo, agent_session, all_rows, [item_id], reason)


def _save_unresolved_draft(
    repo: IntelRepository,
    agent_session: IntelAgentSession,
    rows_by_item: Mapping[int, IntelCandidateAdmission],
    item_ids: Sequence[int],
    reason: str,
) -> Mapping[str, Any]:
    ids = [int(value) for value in item_ids]
    primary = rows_by_item[ids[0]].item
    key = "unresolved-" + "-".join(str(value) for value in ids)
    try:
        draft = repo.upsert_agent_draft(
            int(agent_session.id),
            draft_key=key,
            item_ids=ids,
            title=primary.title,
            summary_cn=(primary.ai_review.summary_cn if primary.ai_review is not None else primary.summary) or primary.title,
            topic=(primary.ai_review.topic if primary.ai_review is not None else "technology_insight") or "technology_insight",
            keywords=(primary.ai_review.keywords if primary.ai_review is not None else ()),
            entities=(primary.ai_review.entities if primary.ai_review is not None else ()),
            novelty_status="uncertain",
            prior_event_key=None,
            review_state="needs_review",
            risk_flags=["needs_review", reason],
            metadata={
                "event_family_key": _normalize_event_family_key(key),
                "facts": [],
                "history_status": "uncertain",
                "publishability": "needs_review",
                "split_reason": None,
                "caveats": [reason],
                "reason": reason,
            },
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "draft_key": draft.draft_key, "draft_id": int(draft.id), "_finalized": False}


def _validate_agent_drafts(
    repo: IntelRepository,
    agent_session: IntelAgentSession,
    active_ids: set[int],
    *,
    admissions: Mapping[str, Sequence[IntelCandidateAdmission]] | None = None,
) -> dict[str, Any]:
    drafts = repo.list_agent_drafts(int(agent_session.id))
    seen: set[int] = set()
    errors: list[str] = []
    for draft in drafts:
        if not draft.members:
            errors.append(f"draft {draft.draft_key} has no members")
        for member in draft.members:
            item_id = int(member.item_id)
            if item_id in seen:
                errors.append(f"item {item_id} appears in more than one draft")
            seen.add(item_id)
    missing = sorted(active_ids - seen)
    if missing:
        errors.append("active candidates are not covered")
    if admissions is not None:
        rows = {int(row.item_id): row for values in admissions.values() for row in values}
        identity_owner: dict[str, str] = {}
        for draft in drafts:
            for member in draft.members:
                item_id = int(member.item_id)
                if item_id not in active_ids or item_id not in rows:
                    continue
                for identity in exact_identity_keys(_item_mapping(rows[item_id].item)):
                    owner = identity_owner.setdefault(identity, draft.draft_key)
                    if owner != draft.draft_key:
                        errors.append(
                            f"active candidates with exact identity {identity} are split across {owner} and {draft.draft_key}"
                        )
    family_errors = _event_family_split_errors(drafts)
    errors.extend(family_errors)
    return {"errors": errors, "missing_active_ids": missing, "draft_count": len(drafts)}


_ALLOWED_STAGE_C_SPLIT_REASONS = frozenset({
    "different_model_or_major_version",
    "separate_time_window_actionable",
    "independent_security_policy_or_breaking_change",
    "platform_released_independent_product",
    "standalone_pricing_quota_access_change",
})


def _event_family_split_errors(drafts: Sequence[IntelEventDraft]) -> list[str]:
    by_family: dict[str, list[IntelEventDraft]] = {}
    for draft in drafts:
        if str(draft.review_state or "").casefold() not in {"candidate", "needs_review"}:
            continue
        metadata = _json_mapping(draft.metadata_json)
        family = _normalize_event_family_key(metadata.get("event_family_key") or draft.draft_key)
        if not family:
            continue
        by_family.setdefault(family, []).append(draft)

    errors: list[str] = []
    for family, rows in sorted(by_family.items()):
        if len(rows) <= 1:
            continue
        missing_or_invalid: list[str] = []
        for draft in rows:
            metadata = _json_mapping(draft.metadata_json)
            reason = str(metadata.get("split_reason") or "").strip()
            if reason not in _ALLOWED_STAGE_C_SPLIT_REASONS:
                missing_or_invalid.append(draft.draft_key)
        if missing_or_invalid:
            errors.append(
                "event_family_key "
                f"{family} has multiple publishable drafts without allowed split_reason: {missing_or_invalid}. "
                "Merge them into one event package, or use one allowed split_reason per remaining draft."
            )
    return errors


def _clear_build_events(session: Session, *, run_id: int) -> None:
    events = list(session.scalars(select(IntelEvent).where(IntelEvent.build_id == int(run_id))).all())
    if not events:
        return
    ids = [int(event.id) for event in events]
    for evidence in session.scalars(select(IntelEventEvidence).where(IntelEventEvidence.event_id.in_(ids))).all():
        evidence.event_id = None
    for event in events:
        session.delete(event)
    session.flush()


def _fail_agent_run(
    *,
    session_factory: sessionmaker[Session],
    run_id: int,
    owner: str,
    error: Exception,
) -> None:
    with session_factory() as failure_session:
        repo = IntelRepository(failure_session)
        stage = repo.get_stage(int(run_id), "cluster")
        if stage is None:
            return
        agent = repo.get_agent_session(int(run_id), stage_name="cluster")
        if agent is not None:
            agent.status = "failed"
            agent.error_code = getattr(error, "error_code", None) or error.__class__.__name__
            agent.error_message = str(error)[:4_000]
            agent.finished_at = datetime.now(timezone.utc)
        task = repo.get_task(stage, subject_type="run", subject_id=int(run_id))
        if task is not None and task.status == "running":
            repo.fail_stage_task(
                task,
                owner=owner,
                error_category="provider" if not isinstance(error, StageCAgentContractError) else "contract",
                error_code=getattr(error, "error_code", None) or "stage_c_agent_failed",
                error_message=str(error),
                retryable=not isinstance(error, StageCAgentContractError),
                raw_response={"agent_session_id": int(agent.id) if agent is not None else None},
            )
        failure_session.commit()


def _assert_downstream_idle(repo: IntelRepository, run_id: int) -> None:
    try:
        repo.assert_stages_idle(int(run_id), stage_names=("stage_d", "export"), upstream_stage="cluster")
    except RuntimeError as exc:
        if str(exc).startswith("downstream_stage_busy:"):
            raise StageCDownstreamBusyError(str(exc)) from exc
        raise


def _result_from_task(result: EventClusterResult, task: IntelRunStageTask) -> EventClusterResult:
    stored = _mapping(task.result)
    result.event_ids = _event_id_list(stored.get("event_ids"))
    result.current_event_ids = _event_id_list(stored.get("current_event_ids"))
    result.candidate_event_ids = _event_id_list(stored.get("candidate_event_ids"))
    result.input_audit = _mapping(stored.get("input_audit"))
    result.turns = _bounded_int(stored.get("turns"))
    result.tool_calls = _bounded_int(stored.get("tool_calls"))
    result.web_searches = _bounded_int(stored.get("web_searches"))
    result.unresolved = _bounded_int(stored.get("unresolved"))
    return result


def _task_result(result: EventClusterResult, agent_session: IntelAgentSession) -> dict[str, Any]:
    return {
        "schema_version": STAGE_C_CANDIDATE_CONTRACT_VERSION,
        "event_ids": result.event_ids,
        "current_event_ids": result.current_event_ids,
        "candidate_event_ids": result.candidate_event_ids,
        "processed": result.processed,
        "input_audit": result.input_audit,
        "agent_session_id": int(agent_session.id),
        "turns": result.turns or int(agent_session.turn_count),
        "tool_calls": result.tool_calls or int(agent_session.tool_call_count),
        "web_searches": result.web_searches or int(agent_session.web_search_count),
        "unresolved": result.unresolved,
    }


def _compact_admission(admission: IntelCandidateAdmission) -> dict[str, Any]:
    item = admission.item
    review = item.ai_review
    return {
        "id": int(item.id),
        "bucket": admission.decision,
        "rank": admission.rank,
        "guarded_score": int(admission.guarded_score),
        "title": item.title,
        "summary_cn": review.summary_cn if review is not None else item.summary,
        "topic": review.topic if review is not None else None,
        "keywords": review.keywords if review is not None else [],
        "entities": review.entities if review is not None else [],
        "canonical_url": item.canonical_url or item.source_url,
        "published_at": _iso_datetime(item.published_at or item.captured_at),
        "source": {
            "id": item.source_id,
            "group": getattr(item.source, "source_group", None),
            "content_class": item.content_class,
        },
    }


def _full_admission(admission: IntelCandidateAdmission) -> dict[str, Any]:
    compact = _compact_admission(admission)
    item = admission.item
    compact["content_text"] = (item.content_text or item.summary or item.title or "")[:16_000]
    compact["source_url"] = item.source_url
    compact["external_id"] = item.external_id
    compact["content_class"] = item.content_class
    compact["metrics"] = _json_mapping(item.metrics_json)
    return compact


def _candidate_matches(item: IntelItem, tokens: Sequence[str]) -> bool:
    review = item.ai_review
    haystack = " ".join(
        [
            item.title or "",
            item.summary or "",
            item.content_text or "",
            review.summary_cn if review is not None else "",
            " ".join(review.keywords) if review is not None else "",
            " ".join(str(entity.get("name") or "") for entity in review.entities) if review is not None else "",
        ]
    ).casefold()
    return all(token in haystack for token in tokens)


def _history_matches(row: Mapping[str, Any], tokens: Sequence[str]) -> bool:
    haystack = " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("summary") or ""),
            " ".join(_strings(row.get("keywords"))),
            " ".join(str(entity.get("name") or "") for entity in row.get("entities", []) if isinstance(entity, Mapping)),
        ]
    ).casefold()
    return all(token in haystack for token in tokens)


def _history_identity_index(history: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for row in history:
        event_key = _text(row.get("event_key"))
        if not event_key:
            continue
        identities: set[str] = set()
        if event_key.startswith(("url:", "external:")):
            identities.add(event_key)
        for ref in row.get("source_refs") or ():
            if not isinstance(ref, Mapping):
                continue
            identities.update(exact_identity_keys(ref))
        for identity in identities:
            bucket = result.setdefault(identity, [])
            if event_key not in bucket:
                bucket.append(event_key)
    return result


def _prepare_draft_history(
    draft: Mapping[str, Any],
    *,
    item_ids: Sequence[int],
    admissions: Mapping[int, IntelCandidateAdmission],
    history_by_key: Mapping[str, Mapping[str, Any]],
    history_identity_index: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    exact_prior_keys: list[str] = []
    for item_id in item_ids:
        for identity in exact_identity_keys(_item_mapping(admissions[int(item_id)].item)):
            for event_key in history_identity_index.get(identity, ()):
                if event_key not in exact_prior_keys:
                    exact_prior_keys.append(event_key)
    requested_prior = _text(draft.get("prior_event_key"))
    requested_status = str(draft.get("history_status") or "uncertain").casefold()
    risk_flags: list[str] = []
    guard: dict[str, Any] = {
        "requested_status": requested_status,
        "requested_prior_event_key": requested_prior,
        "exact_history_matches": exact_prior_keys,
        "history_scope": "previous_three_published_editions",
    }

    if requested_prior and requested_prior not in history_by_key:
        prior_event_key = None
        history_status = "uncertain"
        risk_flags.append("prior_event_outside_history_window")
    else:
        prior_event_key = requested_prior or (exact_prior_keys[0] if exact_prior_keys else None)
        if prior_event_key:
            history_status = (
                "meaningful_update"
                if requested_status == "meaningful_update" and draft.get("facts")
                else "repeat"
            )
        elif requested_status in {"repeat", "meaningful_update"}:
            history_status = "uncertain"
            risk_flags.append("history_match_not_found")
        elif requested_status in {"new", "uncertain"}:
            history_status = requested_status
        else:
            history_status = "uncertain"
            risk_flags.append("invalid_novelty_status")
    novelty_status = "updated" if history_status == "meaningful_update" else history_status
    guard["applied_status"] = history_status
    guard["applied_prior_event_key"] = prior_event_key
    return {
        "history_status": history_status,
        "novelty_status": novelty_status,
        "prior_event_key": prior_event_key,
        "risk_flags": risk_flags,
        "guard": guard,
    }


def _prepare_draft_publishability(
    draft: Mapping[str, Any],
    *,
    item_ids: Sequence[int],
    novelty_status: str,
) -> dict[str, Any]:
    """Validate the simplified event-package publishability contract."""

    facts: list[dict[str, Any]] = []
    for raw in draft.get("facts") or ():
        if not isinstance(raw, Mapping):
            raise ValueError("facts must contain objects")
        claim = _text(raw.get("claim"))
        if not claim:
            raise ValueError("every fact requires a claim")
        supporting = _unique_positive_ids(raw.get("supporting_item_ids"), limit=40)
        outside = [item_id for item_id in supporting if item_id not in item_ids]
        if outside:
            raise ValueError(f"fact references non-member item ids: {outside}")
        if not supporting:
            raise ValueError("every fact requires supporting_item_ids")
        facts.append(
            {
                "claim": claim,
                "supporting_item_ids": supporting,
            }
        )

    requested_publishability = str(draft.get("publishability") or "candidate").casefold()
    risk_flags: list[str] = []
    if requested_publishability not in {"candidate", "needs_review", "rejected"}:
        review_state = "needs_review"
        applied_publishability = "needs_review"
        risk_flags.append("invalid_publishability")
    else:
        review_state = requested_publishability
        applied_publishability = requested_publishability

    normalized_novelty_status = str(novelty_status).casefold()
    if normalized_novelty_status == "repeat":
        review_state = "rejected"
        applied_publishability = "rejected"
        risk_flags.append("confirmed_repeat_without_material_change")
    elif applied_publishability == "candidate" and not facts:
        review_state = "needs_review"
        applied_publishability = "needs_review"
        risk_flags.append("candidate_without_facts")
    elif applied_publishability == "needs_review":
        risk_flags.append("uncertain_event_core")

    guard = {
        "requested_publishability": requested_publishability,
        "applied_publishability": applied_publishability,
        "applied_review_state": review_state,
        "novelty_status": normalized_novelty_status,
        "fact_count": len(facts),
        "policy": "event_package_publishability_v1",
    }
    return {
        "publishability": applied_publishability,
        "facts": facts,
        "review_state": review_state,
        "risk_flags": risk_flags,
        "guard": guard,
    }


def _select_primary_item(items: Sequence[IntelItem]) -> IntelItem:
    if not items:
        raise ValueError("agent draft has no items")
    return min(items, key=_primary_sort_key)


def _primary_sort_key(item: IntelItem) -> tuple[int, int, float, int]:
    source = item.source
    group = str(getattr(source, "source_group", "") or "").casefold()
    if group in {"official_blog", "official_research", "x_official", "github_release"}:
        source_rank = 0
    else:
        source_rank = {
            "news_media": 1,
            "project_tool": 2,
            "community_social": 3,
        }.get(str(item.content_class or "").casefold(), 4)
    timestamp = _as_utc(item.published_at or item.captured_at)
    return (
        source_rank,
        -int(item.b1_priority or 0),
        -(timestamp.timestamp() if timestamp is not None else 0.0),
        int(item.id),
    )


def _member_relation(item: IntelItem, primary: IntelItem) -> str:
    return "duplicate" if set(exact_identity_keys(_item_mapping(item))) & set(exact_identity_keys(_item_mapping(primary))) else "related"


def _stage_c_config_fingerprint(
    *,
    model: str,
    max_turns: int,
    max_tool_calls: int,
    max_web_searches: int,
    lease_seconds: int,
    search_provider: str,
) -> str:
    payload = {
        "agent_version": STAGE_C_AGENT_VERSION,
        "prompt_version": STAGE_C_AGENT_PROMPT_VERSION,
        "candidate_contract_version": STAGE_C_CANDIDATE_CONTRACT_VERSION,
        "primary_policy_version": _PRIMARY_POLICY_VERSION,
        "verification_policy_version": _VERIFICATION_POLICY_VERSION,
        "model": model,
        "max_turns": max_turns,
        "max_tool_calls": max_tool_calls,
        "max_web_searches": max_web_searches,
        "lease_seconds": lease_seconds,
        "search_provider": search_provider,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _cluster_input_fingerprint(
    admissions: Mapping[str, Sequence[IntelCandidateAdmission]],
    history: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        key: [
            {
                "item_id": int(row.item_id),
                "score": int(row.guarded_score),
                "rank": row.rank,
                "policy": row.policy_fingerprint,
            }
            for row in value
        ]
        for key, value in admissions.items()
    }
    payload["history"] = list(history)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _item_mapping(item: IntelItem) -> dict[str, Any]:
    return {
        "id": int(item.id),
        "canonical_url": item.canonical_url,
        "source_url": item.source_url,
        "external_id": item.external_id,
        "title": item.title,
    }


def _event_family_key(draft: Mapping[str, Any]) -> str:
    return _normalize_event_family_key(draft.get("event_family_key") or draft.get("draft_key"))


def _normalize_event_family_key(value: Any) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().casefold()).strip("_")
    return text[:120] or "other"


def _normalize_external_id(value: Any) -> str | None:
    text = re.sub(r"\s+", "", str(value).strip()).casefold() if value is not None else ""
    return text or None


def _parse_call_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"raw_arguments": value}
        return dict(parsed) if isinstance(parsed, Mapping) else {"raw_arguments": value}
    return {}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _unique_positive_ids(value: Any, *, limit: int) -> list[int]:
    values = value if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)) else ()
    result: list[int] = []
    for raw in values:
        try:
            item_id = int(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if item_id > 0 and item_id not in result:
            result.append(item_id)
        if len(result) >= limit:
            break
    return result


def _event_id_list(value: Any) -> list[int]:
    return _unique_positive_ids(value, limit=10_000)


def _json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        value = parsed
    return _strings(value)


def _json_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    values = value if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)) else ()
    return _unique_strings(values)


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return result


def _unique_json_objects(values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        row = dict(value)
        key = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="python"))
    return {}


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _bounded_score(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if parsed > 0 else default


def _stage_c_lease_seconds(ai_client: Any, *, max_turns: int) -> int:
    """Keep C leased for its full multi-turn model budget plus persistence slack."""

    try:
        timeout_seconds = float(getattr(ai_client, "timeout_seconds", 30.0))
    except (TypeError, ValueError, OverflowError):
        timeout_seconds = 30.0
    timeout_seconds = max(1.0, timeout_seconds)
    estimated = int(math.ceil(timeout_seconds * max(1, int(max_turns)) + 120.0))
    return max(600, min(7_200, estimated))


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso_datetime(value: Any) -> str | None:
    current = _as_utc(value)
    return current.isoformat() if current is not None else None


__all__ = [
    "EventClusterResult",
    "StageCAgentContractError",
    "StageCDownstreamBusyError",
    "canonical_event_key",
    "canonical_event_url",
    "exact_identity_keys",
    "normalize_event_title",
    "run_event_cluster_from_settings",
    "run_event_cluster_job",
]
