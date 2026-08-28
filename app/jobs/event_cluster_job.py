"""Stage C: plan/commit Responses agent for event-level aggregation.

The model researches candidates with read-only tools and submits one complete
event plan. Deterministic code owns identity normalization, coverage repair,
atomic draft replacement, score admission contracts, and downstream materialization.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ai.responses import AgentBudgetExceeded, AgentProtocolError, FunctionTool
from app.ai.skills.event_package import build_candidate_event_package
from app.ai.skills.intel_triage import normalize_url
from app.ai.skills.stage_c_agent import StageCAgentClient
from app.ai.skills.stage_c_agent.prompts import (
    ATTACH_SEARCH_EVIDENCE_SCHEMA,
    LIST_CANDIDATES_SCHEMA,
    LIST_PLAN_SNAPSHOT_SCHEMA,
    READ_HISTORY_SCHEMA,
    READ_ITEMS_SCHEMA,
    SEARCH_CANDIDATES_SCHEMA,
    SEARCH_WEB_SCHEMA,
    STAGE_C_AGENT_PROMPT_VERSION,
    SUBMIT_EVENT_PLAN_SCHEMA,
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
    IntelAgentSession,
    IntelCandidateAdmission,
    IntelEvent,
    IntelEventDraft,
    IntelEventEvidence,
    IntelItem,
    IntelRun,
    IntelRunStageTask,
)
from app.storage.repository import IntelRepository


DAILY_HISTORY_DAYS = DEFAULT_STAGE_C_AGENT_HISTORY_DAYS
STAGE_C_CANDIDATE_CONTRACT_VERSION = "stage_c_events_v9"
_TRACKING_QUERY_KEYS = {"ref", "source", "src", "campaign", "fbclid", "gclid", "mc_cid", "mc_eid"}
_PRIMARY_POLICY_VERSION = "source_then_b1_priority_v2"
_VERIFICATION_POLICY_VERSION = "tavily_per_event_verification_v3"
_GITHUB_REPO_ROOT_RE = re.compile(r"^https?://(www\.)?github\.com/[^/]+/[^/]+/?$", re.IGNORECASE)
ProgressCallback = Callable[[dict[str, Any]], None]


class StageCDownstreamBusyError(RuntimeError):
    """A live Stage-D/export worker prevents safe Stage-C replacement."""


class StageCAgentContractError(RuntimeError):
    """The model tried to commit an invalid event plan."""


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


def _normalize_external_id(value: Any) -> str | None:
    text = re.sub(r"\s+", "", str(value).strip()).casefold() if value is not None else ""
    return text or None


def _is_github_repo_root_url(url: str | None) -> bool:
    if not url:
        return False
    return bool(_GITHUB_REPO_ROOT_RE.match(url.rstrip("/")))


def exact_identity_keys(value: Any) -> tuple[str, ...]:
    """Strong exact-identity keys used for forced merge / history matching.

    GitHub Release items are identified by ``github_release:*`` external IDs.
    A shared repository homepage URL is not treated as the same exact identity
    across different releases or repo cards.
    """

    values = _mapping(value)
    external_id = _normalize_external_id(values.get("external_id") or values.get("guid"))
    url = canonical_event_url(values.get("canonical_url") or values.get("url") or values.get("source_url"))
    keys: list[str] = []
    if external_id:
        keys.append(f"external:{external_id}")
        if external_id.startswith("github_release:"):
            return tuple(keys)
        if external_id.startswith("github_repo:"):
            return tuple(keys)
    if url and not _is_github_repo_root_url(url):
        keys.append(f"url:{url}")
    elif url and not external_id:
        # Non-GitHub-identified rows may still use a bare repo root URL.
        keys.append(f"url:{url}")
    return tuple(dict.fromkeys(keys))


def related_identity_hints(value: Any) -> tuple[str, ...]:
    """Soft relatedness hints that never force cross-event merges alone."""

    values = _mapping(value)
    external_id = _normalize_external_id(values.get("external_id") or values.get("guid"))
    url = canonical_event_url(values.get("canonical_url") or values.get("url") or values.get("source_url"))
    hints: list[str] = []
    if url and _is_github_repo_root_url(url):
        hints.append(f"github_repo_root:{url.rstrip('/').casefold()}")
    if external_id and external_id.startswith("github_release:") and url:
        hints.append(f"url:{url}")
    return tuple(dict.fromkeys(hints))


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
    progress: ProgressCallback | None = None,
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
                "aggregation_mode": "responses_agent_plan_commit_v1",
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
                    "protocol": "plan_commit_v1",
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
            _emit_progress(
                progress,
                "stage_update",
                stage="cluster",
                data={
                    "total": len(admissions["active"]),
                    "current": _covered_active_count_from_plan(tools.accepted_plan, tools.active_ids),
                    "metrics": {
                        "input_items": len(admissions["active"]),
                        "reserve_items": len(admissions["reserve"]),
                        "history_events": len(history),
                    },
                    "current_action": "prepare_workspace",
                },
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
                _emit_progress(
                    progress,
                    "stage_c_response",
                    stage="cluster",
                    data={"turn": int(turn), "response_id": _text(response.get("id"))},
                )

            def on_tool(turn: int, call: Mapping[str, Any], output: Mapping[str, Any]) -> None:
                arguments = _parse_call_arguments(call.get("arguments"))
                if repo.heartbeat_stage_task(task, owner=owner, lease_seconds=lease_seconds) is None:
                    raise StageCLeaseLostError("Stage C task lease was lost while saving an agent tool call")
                repo.append_agent_step(
                    int(agent_session.id),
                    turn=turn,
                    kind="tool_call",
                    tool_name=_text(call.get("name")),
                    call_id=_text(call.get("call_id")),
                    input_value=arguments,
                    output_value=output,
                    status="success" if output.get("ok", True) else "error",
                    error_message=_text(output.get("error")) or _join_errors(output.get("errors")),
                )
                agent_session.tool_call_count += 1
                if str(call.get("name") or "") == "search_web":
                    agent_session.web_search_count += 1
                session.commit()
                _emit_progress(
                    progress,
                    "stage_c_tool",
                    stage="cluster",
                    data=_stage_c_tool_progress(
                        tools,
                        tool_name=_text(call.get("name")),
                        arguments=arguments,
                        output=output,
                    ),
                )

            if not agent_session.finalization_requested:
                agent_result = ai_client.run(
                    initial_context={
                        "run_id": int(run_id),
                        "reference_time": current.isoformat(),
                        "active_candidate_count": len(admissions["active"]),
                        "reserve_candidate_count": len(admissions["reserve"]),
                        "history_window_days": DAILY_HISTORY_DAYS,
                        "protocol": "plan_commit_v1",
                        "instructions": (
                            "Use local read-only tools to inspect every active candidate. Compare only the previous "
                            "three published editions for novelty. Use search_web for material uncertainty on "
                            "needs_review events. Submit one complete event plan with submit_event_plan that covers "
                            "every active candidate. If validation fails, resubmit the full corrected plan."
                        ),
                    },
                    function_tools=tools.function_tools,
                    max_turns=max_turns,
                    max_tool_calls=max_tool_calls,
                    on_response=on_response,
                    on_tool=on_tool,
                )
                result.turns = agent_result.turns
                result.tool_calls = agent_result.tool_calls
                result.web_searches = int(agent_session.web_search_count)
            if repo.heartbeat_stage_task(task, owner=owner, lease_seconds=lease_seconds) is None:
                raise StageCLeaseLostError("Stage C task lease was lost before event materialization")
            if not agent_session.finalization_requested:
                raise AgentProtocolError("C agent did not submit an accepted event plan")
            _ensure_committed_drafts(
                repo=repo,
                agent_session=agent_session,
                tools=tools,
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
            # needs-review singleton events, then commit a valid projection.
            tools.commit_fallback_plan(reason="agent_budget_exhausted")
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
    progress: ProgressCallback | None = None,
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
        progress=progress,
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
        self.search_attempted_event_keys: set[str] = set()
        self.search_calls = 0
        self.admissions = {key: list(value) for key, value in admissions.items()}
        self.by_item_id = {
            int(row.item_id): row
            for rows in self.admissions.values()
            for row in rows
        }
        self.active_ids = {int(row.item_id) for row in self.admissions.get("active", ())}
        self.accepted_plan: list[dict[str, Any]] = []
        self.last_validation: dict[str, Any] = {"errors": [], "events": [], "missing_active_ids": []}
        self._restore_search_state()
        self._restore_plan_state()

    @property
    def function_tools(self) -> list[FunctionTool]:
        return [
            FunctionTool("list_candidates", "分页列出 B 已准入的 active 或 reserve 候选概要。", LIST_CANDIDATES_SCHEMA, self.list_candidates),
            FunctionTool("list_plan_snapshot", "列出最近一次校验或已接受的事件方案快照。", LIST_PLAN_SNAPSHOT_SCHEMA, self.list_plan_snapshot),
            FunctionTool("read_items", "读取候选的完整原文、B 分析和来源元数据。", READ_ITEMS_SCHEMA, self.read_items),
            FunctionTool("search_candidates", "在 B 准入候选内按词检索相关内容。", SEARCH_CANDIDATES_SCHEMA, self.search_candidates),
            FunctionTool("read_recent_history", "查询过去三天的已发布日报事件，判断重复或更新。", READ_HISTORY_SCHEMA, self.read_recent_history),
            FunctionTool("search_web", "通过 Tavily 搜索公开网页并返回可审计的来源结果。", SEARCH_WEB_SCHEMA, self.search_web),
            FunctionTool("attach_search_evidence", "把 Tavily 结果绑定到 event_key 和具体核验 claim。", ATTACH_SEARCH_EVIDENCE_SCHEMA, self.attach_search_evidence),
            FunctionTool("submit_event_plan", "提交覆盖全部 active 的完整事件方案，由本地校验并原子落库。", SUBMIT_EVENT_PLAN_SCHEMA, self.submit_event_plan),
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

    def list_plan_snapshot(self, args: dict[str, Any]) -> Mapping[str, Any]:
        del args
        events = self.accepted_plan or self.last_validation.get("events") or []
        return {
            "ok": True,
            "accepted": bool(self.accepted_plan),
            "event_count": len(events),
            "covered_active_ids": sorted(
                {
                    int(item_id)
                    for event in events
                    for item_id in event.get("item_ids") or ()
                    if int(item_id) in self.active_ids
                }
            ),
            "missing_active_ids": list(self.last_validation.get("missing_active_ids") or []),
            "errors": list(self.last_validation.get("errors") or []),
            "events": [_plan_event_view(event) for event in events],
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

    def search_web(self, args: dict[str, Any]) -> Mapping[str, Any]:
        event_key = _text(args.get("event_key"))
        if event_key:
            self.search_attempted_event_keys.add(event_key)
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
            "event_key": event_key,
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
        event_key = _text(args.get("event_key"))
        if not event_key:
            return {"ok": False, "error": "event_key is required"}
        try:
            evidence = self.repo.record_agent_evidence(
                int(self.agent_session.id),
                event_key=event_key,
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
        self.search_attempted_event_keys.add(event_key)
        self.session.commit()
        return {
            "ok": True,
            "evidence_id": int(evidence.id),
            "result_id": result_id,
            "event_key": event_key,
            "host": evidence.host,
            "status": status,
        }

    def submit_event_plan(self, args: dict[str, Any]) -> Mapping[str, Any]:
        raw_events = args.get("events")
        if not isinstance(raw_events, list) or not raw_events:
            return {"ok": False, "error": "events must be a non-empty list", "errors": ["events must be a non-empty list"]}

        prepared, errors = _prepare_event_plan(
            raw_events,
            active_ids=self.active_ids,
            admissions=self.by_item_id,
            history_by_key=self.history_by_key,
            history_identity_index=self.history_identity_index,
            search_attempted_event_keys=self.search_attempted_event_keys,
            max_web_searches=self.max_web_searches,
            search_configured=bool(self.search_client is not None and self.search_client.is_configured),
        )
        self.last_validation = {
            "errors": list(errors),
            "events": [dict(event) for event in prepared],
            "missing_active_ids": sorted(
                self.active_ids
                - {
                    int(item_id)
                    for event in prepared
                    for item_id in event.get("item_ids") or ()
                }
            ),
        }
        if errors:
            return {
                "ok": False,
                "errors": errors,
                "missing_active_ids": self.last_validation["missing_active_ids"],
                "event_count": len(prepared),
                "next_action": (
                    "Fix the listed validation errors and resubmit one complete event plan covering every active item."
                ),
            }

        try:
            with self.session.begin_nested():
                self.repo.replace_agent_drafts(
                    int(self.agent_session.id),
                    [_draft_persistence_spec(event) for event in prepared],
                )
                self.repo.reattach_agent_evidence_by_event_keys(int(self.agent_session.id))
            self.session.commit()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "errors": [str(exc)]}

        self.accepted_plan = [dict(event) for event in prepared]
        self.last_validation = {
            "errors": [],
            "events": self.accepted_plan,
            "missing_active_ids": [],
        }
        self.agent_session.finalization_requested = True
        self.agent_session.status = "finalizing"
        state = dict(self.agent_session.state or {})
        state["accepted_plan"] = self.accepted_plan
        state["search_attempted_event_keys"] = sorted(self.search_attempted_event_keys)
        self.agent_session.state_json = json.dumps(state, ensure_ascii=False)
        self.session.commit()
        return {
            "ok": True,
            "event_count": len(self.accepted_plan),
            "covered_active_count": len(self.active_ids),
            "_finalized": True,
        }

    def commit_fallback_plan(self, *, reason: str) -> None:
        """Build a valid plan from accepted events plus unresolved gaps."""

        seed = [dict(event) for event in self.accepted_plan]
        prepared, _errors = _prepare_event_plan(
            seed,
            active_ids=self.active_ids,
            admissions=self.by_item_id,
            history_by_key=self.history_by_key,
            history_identity_index=self.history_identity_index,
            search_attempted_event_keys=self.search_attempted_event_keys,
            max_web_searches=0,
            search_configured=False,
            auto_fill_missing=True,
            unresolved_reason=reason,
        )
        self.repo.replace_agent_drafts(
            int(self.agent_session.id),
            [_draft_persistence_spec(event) for event in prepared],
        )
        self.repo.reattach_agent_evidence_by_event_keys(int(self.agent_session.id))
        self.accepted_plan = prepared
        self.agent_session.finalization_requested = True
        self.agent_session.status = "finalizing"
        state = dict(self.agent_session.state or {})
        state["accepted_plan"] = prepared
        state["fallback_reason"] = reason
        self.agent_session.state_json = json.dumps(state, ensure_ascii=False)
        self.session.commit()

    def _restore_search_state(self) -> None:
        state = dict(self.agent_session.state or {})
        for key in state.get("search_attempted_event_keys") or ():
            text = _text(key)
            if text:
                self.search_attempted_event_keys.add(text)
        for step in self.agent_session.steps:
            if step.kind != "tool_call" or step.tool_name != "search_web":
                continue
            self.search_calls += 1
            input_value = _json_mapping(step.input_json)
            output_value = _json_mapping(step.output_json)
            event_key = _text(input_value.get("event_key") or output_value.get("event_key") or input_value.get("draft_key"))
            if event_key:
                self.search_attempted_event_keys.add(event_key)
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

    def _restore_plan_state(self) -> None:
        state = dict(self.agent_session.state or {})
        plan = state.get("accepted_plan")
        if isinstance(plan, list) and plan and self.agent_session.finalization_requested:
            self.accepted_plan = [dict(row) for row in plan if isinstance(row, Mapping)]
            self.last_validation = {"errors": [], "events": self.accepted_plan, "missing_active_ids": []}


def _prepare_event_plan(
    raw_events: Sequence[Any],
    *,
    active_ids: set[int],
    admissions: Mapping[int, IntelCandidateAdmission],
    history_by_key: Mapping[str, Mapping[str, Any]],
    history_identity_index: Mapping[str, Sequence[str]],
    search_attempted_event_keys: set[str],
    max_web_searches: int,
    search_configured: bool,
    auto_fill_missing: bool = False,
    unresolved_reason: str = "needs_review",
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    prepared: list[dict[str, Any]] = []
    assigned: dict[int, str] = {}
    keys: set[str] = set()

    for index, raw_value in enumerate(raw_events):
        if not isinstance(raw_value, Mapping):
            errors.append(f"events[{index}] must be an object")
            continue
        event_args = dict(raw_value)
        event_key = _text(event_args.get("event_key") or event_args.get("draft_key"))
        if not event_key:
            errors.append(f"events[{index}] is missing event_key")
            continue
        if event_key in keys:
            errors.append(f"duplicate event_key in plan: {event_key}")
            continue
        keys.add(event_key)
        ids = _unique_positive_ids(event_args.get("item_ids"), limit=40)
        if not ids:
            errors.append(f"event {event_key} requires item_ids")
            continue
        unknown = [item_id for item_id in ids if item_id not in admissions]
        if unknown:
            errors.append(f"event {event_key} references items outside the C workbench: {unknown}")
            continue
        conflict = False
        for item_id in ids:
            prior_key = assigned.get(item_id)
            if prior_key is not None:
                errors.append(f"item {item_id} appears in both {prior_key} and {event_key}")
                conflict = True
                break
            assigned[item_id] = event_key
        if conflict:
            continue
        try:
            history = _prepare_draft_history(
                event_args,
                item_ids=ids,
                admissions=admissions,
                history_by_key=history_by_key,
                history_identity_index=history_identity_index,
            )
            publishability = _prepare_draft_publishability(
                event_args,
                item_ids=ids,
                novelty_status=str(history["novelty_status"]),
            )
        except ValueError as exc:
            errors.append(f"event {event_key}: {exc}")
            continue
        caveats = _strings(event_args.get("caveats"))
        caveats.extend(value for value in history["risk_flags"] if value not in caveats)
        caveats.extend(value for value in publishability["risk_flags"] if value not in caveats)
        prepared.append(
            {
                "event_key": event_key,
                "draft_key": event_key,
                "item_ids": ids,
                "title": str(event_args.get("title") or "").strip() or f"event-{event_key}",
                "summary_cn": _text(event_args.get("summary_cn")),
                "topic": str(event_args.get("topic") or "technology_insight"),
                "facts": publishability["facts"],
                "history_status": history["history_status"],
                "novelty_status": history["novelty_status"],
                "prior_event_key": history.get("prior_event_key"),
                "publishability": publishability["publishability"],
                "review_state": publishability["review_state"],
                "split_reason": _text(event_args.get("split_reason")),
                "caveats": caveats,
                "event_family_key": _event_family_key(event_args),
                "history_guard": history["guard"],
                "publishability_guard": publishability["guard"],
            }
        )

    prepared = _merge_exact_identity_events(prepared, admissions=admissions)
    prepared, identity_notes = _dedupe_item_assignments(prepared)
    for note in identity_notes:
        if note not in errors and not auto_fill_missing:
            # Notes are informational after automatic merge; only surface as
            # soft caveats on the surviving events.
            pass

    covered = {
        int(item_id)
        for event in prepared
        for item_id in event.get("item_ids") or ()
    }
    missing = sorted(active_ids - covered)
    if missing and not auto_fill_missing:
        errors.append(f"active candidates are not covered: {missing}")
    if missing and auto_fill_missing:
        for item_id in missing:
            prepared.append(_unresolved_event_spec(admissions[item_id], reason=unresolved_reason))

    family_errors = _event_family_split_errors_from_plan(prepared)
    errors.extend(family_errors)

    if max_web_searches > 0 and search_configured and not auto_fill_missing:
        pending = [
            event["event_key"]
            for event in prepared
            if str(event.get("publishability") or "").casefold() == "needs_review"
            and event["event_key"] not in search_attempted_event_keys
        ]
        if pending:
            errors.append(
                "each needs_review event requires its own Tavily verification pass before commit: "
                + ", ".join(pending)
            )

    # Exact-identity splits across publishable events should already be merged.
    # Keep a final guard that only fails if merge could not collapse them.
    identity_errors = _publishable_identity_split_errors(prepared, admissions=admissions, active_ids=active_ids)
    errors.extend(identity_errors)

    if errors and not auto_fill_missing:
        return prepared, errors
    if auto_fill_missing:
        # Fallback path must always produce a commitable projection.
        return prepared, []
    return prepared, errors


def _merge_exact_identity_events(
    events: Sequence[Mapping[str, Any]],
    *,
    admissions: Mapping[int, IntelCandidateAdmission],
) -> list[dict[str, Any]]:
    """Force-merge publishable events that share a strong exact identity."""

    working = [dict(event) for event in events]
    if len(working) <= 1:
        return working

    def event_identities(event: Mapping[str, Any]) -> set[str]:
        if str(event.get("publishability") or event.get("review_state") or "").casefold() == "rejected":
            return set()
        keys: set[str] = set()
        for item_id in event.get("item_ids") or ():
            row = admissions.get(int(item_id))
            if row is None:
                continue
            keys.update(exact_identity_keys(_item_mapping(row.item)))
        return keys

    changed = True
    while changed and len(working) > 1:
        changed = False
        identity_owner: dict[str, int] = {}
        merge_pair: tuple[int, int] | None = None
        for index, event in enumerate(working):
            for identity in event_identities(event):
                owner = identity_owner.get(identity)
                if owner is None:
                    identity_owner[identity] = index
                    continue
                if owner != index:
                    merge_pair = (owner, index)
                    break
            if merge_pair is not None:
                break
        if merge_pair is None:
            break
        left_idx, right_idx = merge_pair
        left = working[left_idx]
        right = working[right_idx]
        survivor, absorbed = _prefer_event(left, right)
        left_ids = list(survivor.get("item_ids") or ())
        right_ids = list(absorbed.get("item_ids") or ())
        survivor = dict(survivor)
        survivor["item_ids"] = list(dict.fromkeys([*left_ids, *right_ids]))
        survivor_facts = list(survivor.get("facts") or [])
        for fact in list(absorbed.get("facts") or ()):
            if fact not in survivor_facts:
                survivor_facts.append(fact)
        survivor["facts"] = survivor_facts
        caveats = list(survivor.get("caveats") or [])
        for value in list(absorbed.get("caveats") or ()):
            if value not in caveats:
                caveats.append(value)
        note = f"merged exact-identity event {absorbed.get('event_key')} into {survivor.get('event_key')}"
        if note not in caveats:
            caveats.append(note)
        survivor["caveats"] = caveats
        if str(survivor.get("publishability")).casefold() == "rejected" and str(absorbed.get("publishability")).casefold() != "rejected":
            survivor["publishability"] = absorbed.get("publishability")
            survivor["review_state"] = absorbed.get("review_state")
        next_events = [event for idx, event in enumerate(working) if idx not in {left_idx, right_idx}]
        next_events.insert(min(left_idx, right_idx), survivor)
        working = next_events
        changed = True
    return working


def _prefer_event(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    rank = {"candidate": 0, "needs_review": 1, "rejected": 2}
    left_rank = rank.get(str(left.get("publishability") or "").casefold(), 9)
    right_rank = rank.get(str(right.get("publishability") or "").casefold(), 9)
    left_size = len(left.get("item_ids") or ())
    right_size = len(right.get("item_ids") or ())
    if (left_rank, -left_size, str(left.get("event_key") or "")) <= (right_rank, -right_size, str(right.get("event_key") or "")):
        return dict(left), dict(right)
    return dict(right), dict(left)


def _dedupe_item_assignments(events: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    claimed: dict[int, str] = {}
    result: list[dict[str, Any]] = []
    for event in events:
        row = dict(event)
        kept: list[int] = []
        for item_id in row.get("item_ids") or ():
            item_id = int(item_id)
            owner = claimed.get(item_id)
            if owner is None:
                claimed[item_id] = str(row.get("event_key"))
                kept.append(item_id)
                continue
            notes.append(f"item {item_id} dropped from {row.get('event_key')} because it already belongs to {owner}")
        if not kept:
            notes.append(f"event {row.get('event_key')} removed because it lost all members")
            continue
        row["item_ids"] = kept
        result.append(row)
    return result, notes


def _publishable_identity_split_errors(
    events: Sequence[Mapping[str, Any]],
    *,
    admissions: Mapping[int, IntelCandidateAdmission],
    active_ids: set[int],
) -> list[str]:
    identity_owner: dict[str, str] = {}
    errors: list[str] = []
    for event in events:
        publishability = str(event.get("publishability") or event.get("review_state") or "").casefold()
        if publishability not in {"candidate", "needs_review"}:
            continue
        event_key = str(event.get("event_key") or "")
        for item_id in event.get("item_ids") or ():
            item_id = int(item_id)
            if item_id not in active_ids or item_id not in admissions:
                continue
            for identity in exact_identity_keys(_item_mapping(admissions[item_id].item)):
                owner = identity_owner.setdefault(identity, event_key)
                if owner != event_key:
                    errors.append(
                        f"active candidates with exact identity {identity} are split across {owner} and {event_key}"
                    )
    return errors


def _event_family_split_errors_from_plan(events: Sequence[Mapping[str, Any]]) -> list[str]:
    by_family: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        if str(event.get("publishability") or event.get("review_state") or "").casefold() not in {"candidate", "needs_review"}:
            continue
        family = _normalize_event_family_key(event.get("event_family_key") or event.get("event_key"))
        if not family:
            continue
        by_family.setdefault(family, []).append(event)

    errors: list[str] = []
    for family, rows in sorted(by_family.items()):
        if len(rows) <= 1:
            continue
        missing_or_invalid: list[str] = []
        for event in rows:
            reason = str(event.get("split_reason") or "").strip()
            if reason not in _ALLOWED_STAGE_C_SPLIT_REASONS:
                missing_or_invalid.append(str(event.get("event_key")))
        if missing_or_invalid:
            errors.append(
                "event_family_key "
                f"{family} has multiple publishable events without allowed split_reason: {missing_or_invalid}. "
                "Merge them into one event package, or use one allowed split_reason per remaining event."
            )
    return errors


def _unresolved_event_spec(admission: IntelCandidateAdmission, *, reason: str) -> dict[str, Any]:
    item = admission.item
    item_id = int(item.id)
    key = f"unresolved-{item_id}"
    title = item.title or f"unresolved-{item_id}"
    summary = (item.ai_review.summary_cn if item.ai_review is not None else item.summary) or title
    topic = (item.ai_review.topic if item.ai_review is not None else "technology_insight") or "technology_insight"
    return {
        "event_key": key,
        "draft_key": key,
        "item_ids": [item_id],
        "title": title,
        "summary_cn": summary,
        "topic": topic,
        "facts": [],
        "history_status": "uncertain",
        "novelty_status": "uncertain",
        "prior_event_key": None,
        "publishability": "needs_review",
        "review_state": "needs_review",
        "split_reason": None,
        "caveats": ["needs_review", reason],
        "event_family_key": _normalize_event_family_key(key),
        "history_guard": {"policy": "fallback_unresolved"},
        "publishability_guard": {"policy": "fallback_unresolved", "reason": reason},
    }


def _draft_persistence_spec(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "draft_key": event["event_key"],
        "item_ids": list(event.get("item_ids") or ()),
        "title": event.get("title"),
        "summary_cn": event.get("summary_cn"),
        "topic": event.get("topic") or "technology_insight",
        "keywords": (),
        "entities": (),
        "novelty_status": event.get("novelty_status") or "uncertain",
        "prior_event_key": event.get("prior_event_key"),
        "review_state": event.get("review_state") or event.get("publishability") or "candidate",
        "risk_flags": list(event.get("caveats") or ()),
        "metadata": {
            "saved_by": "responses_agent_plan_commit",
            "event_family_key": event.get("event_family_key"),
            "facts": list(event.get("facts") or ()),
            "history_status": event.get("history_status"),
            "history_guard": event.get("history_guard") or {},
            "publishability": event.get("publishability"),
            "publishability_guard": event.get("publishability_guard") or {},
            "split_reason": event.get("split_reason"),
            "caveats": list(event.get("caveats") or ()),
        },
    }


def _plan_event_view(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_key": event.get("event_key"),
        "title": event.get("title"),
        "item_ids": list(event.get("item_ids") or ()),
        "event_family_key": event.get("event_family_key"),
        "history_status": event.get("history_status"),
        "publishability": event.get("publishability"),
        "caveats": list(event.get("caveats") or ()),
    }


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


def _ensure_committed_drafts(
    *,
    repo: IntelRepository,
    agent_session: IntelAgentSession,
    tools: _StageCAgentTools,
) -> None:
    """Rewrite drafts from the accepted in-memory plan when resume left them empty."""

    drafts = repo.list_agent_drafts(int(agent_session.id))
    if drafts:
        return
    plan = tools.accepted_plan or list((agent_session.state or {}).get("accepted_plan") or [])
    if not plan:
        raise StageCAgentContractError("Stage C finalized without a committed event plan")
    repo.replace_agent_drafts(
        int(agent_session.id),
        [_draft_persistence_spec(event) for event in plan if isinstance(event, Mapping)],
    )
    repo.reattach_agent_evidence_by_event_keys(int(agent_session.id))
    tools.session.commit()


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
    drafts = repo.list_agent_drafts(int(agent_session.id))
    validation = _validate_committed_drafts(drafts, active_ids=active_ids, admissions=admissions)
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
        for draft in drafts
        for member in draft.members
    }
    for draft in drafts:
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
        # Prefer the committed draft key so distinct plan events stay distinct
        # even when members share a weak repository homepage URL.
        event_key = str(draft.draft_key or "").strip() or canonical_event_key(_item_mapping(primary))
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
            keywords=_unique_strings(review_keywords),
            entities=_unique_json_objects(review_entities),
            content_class=primary.content_class,
            source_group=getattr(primary.source, "source_group", None),
            source_ids=source_ids,
            source_groups=source_groups,
            identity_keys=[key for item in members for key in exact_identity_keys(_item_mapping(item))],
            display_score=max((int(item.b1_priority or 0) for item in members), default=0),
            novelty_status=draft.novelty_status,
            state="candidate",
            review_state=draft.review_state,
            resolution_method="responses_agent_plan_commit",
            resolution_raw={
                "agent_session_id": int(agent_session.id),
                "draft_key": draft.draft_key,
                "event_key": draft.draft_key,
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
        resolution = _json_mapping(event.resolution_raw_json)
        resolution["event_package"] = build_candidate_event_package(event)
        event.resolution_raw_json = json.dumps(resolution, ensure_ascii=False)
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
        if str(draft.review_state or "").casefold() in {"candidate", "needs_review"}:
            result.candidate_event_ids.append(int(event.id))
        if draft.review_state == "needs_review":
            result.unresolved += 1
    session.flush()


def _validate_committed_drafts(
    drafts: Sequence[IntelEventDraft],
    *,
    active_ids: set[int],
    admissions: Mapping[str, Sequence[IntelCandidateAdmission]],
) -> dict[str, Any]:
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
        errors.append(f"active candidates are not covered: {missing}")
    rows = {int(row.item_id): row for values in admissions.values() for row in values}
    plan_events = []
    for draft in drafts:
        metadata = _json_mapping(draft.metadata_json)
        plan_events.append(
            {
                "event_key": draft.draft_key,
                "item_ids": [int(member.item_id) for member in draft.members],
                "publishability": metadata.get("publishability") or draft.review_state,
                "review_state": draft.review_state,
                "split_reason": metadata.get("split_reason"),
                "event_family_key": metadata.get("event_family_key") or draft.draft_key,
            }
        )
    errors.extend(_publishable_identity_split_errors(plan_events, admissions=rows, active_ids=active_ids))
    errors.extend(_event_family_split_errors_from_plan(plan_events))
    return {"errors": errors, "missing_active_ids": missing, "draft_count": len(drafts)}


_ALLOWED_STAGE_C_SPLIT_REASONS = frozenset({
    "different_model_or_major_version",
    "separate_time_window_actionable",
    "independent_security_policy_or_breaking_change",
    "platform_released_independent_product",
    "standalone_pricing_quota_access_change",
})


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


def _emit_progress(
    progress: ProgressCallback | None,
    event_type: str,
    *,
    stage: str | None = None,
    message: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    if progress is None:
        return
    progress({"type": event_type, "stage": stage, "message": message, "data": dict(data or {})})


def _stage_c_tool_progress(
    tools: _StageCAgentTools,
    *,
    tool_name: str | None,
    arguments: Mapping[str, Any],
    output: Mapping[str, Any],
) -> dict[str, Any]:
    events = tools.accepted_plan or tools.last_validation.get("events") or []
    title = _stage_c_current_title(tool_name, arguments, output)
    return {
        "tool": tool_name,
        "ok": bool(output.get("ok", True)),
        "error": _text(output.get("error")) or _join_errors(output.get("errors")),
        "title": title,
        "active_total": len(tools.active_ids),
        "covered_items": _covered_active_count_from_plan(events, tools.active_ids),
        "event_count": len(events),
        "needs_review": sum(
            1 for event in events if str(event.get("publishability") or "").casefold() == "needs_review"
        ),
        "rejected": sum(1 for event in events if str(event.get("publishability") or "").casefold() == "rejected"),
    }


def _covered_active_count_from_plan(events: Sequence[Mapping[str, Any]] | None, active_ids: set[int]) -> int:
    if not events:
        return 0
    covered = {
        int(item_id)
        for event in events
        for item_id in event.get("item_ids") or ()
        if int(item_id) in active_ids
    }
    return len(covered)


def _stage_c_current_title(
    tool_name: str | None,
    arguments: Mapping[str, Any],
    output: Mapping[str, Any],
) -> str | None:
    if tool_name == "read_items":
        items = output.get("items")
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, Mapping):
                return _text(first.get("title"))
    if tool_name == "submit_event_plan":
        return _text(output.get("event_count")) and f"events={output.get('event_count')}"
    if tool_name == "search_web":
        return _text(arguments.get("claim")) or _text(arguments.get("query"))
    if tool_name == "attach_search_evidence":
        return _text(arguments.get("claim")) or _text(arguments.get("event_key"))
    if tool_name == "list_plan_snapshot":
        return f"events={output.get('event_count')}"
    return None


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
        "external_id": item.external_id,
        "identity_keys": list(exact_identity_keys(_item_mapping(item))),
        "related_identity_hints": list(related_identity_hints(_item_mapping(item))),
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
        "protocol": "plan_commit_v1",
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
    return _normalize_event_family_key(draft.get("event_family_key") or draft.get("event_key") or draft.get("draft_key"))


def _normalize_event_family_key(value: Any) -> str:
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().casefold()).strip("_")
    return text[:120] or "other"


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


def _join_errors(value: Any) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    parts = [str(item).strip() for item in value if str(item).strip()]
    return "; ".join(parts) if parts else None


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
    "related_identity_hints",
    "run_event_cluster_from_settings",
    "run_event_cluster_job",
]
