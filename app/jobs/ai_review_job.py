"""Stage A/B AI orchestration for fetched intelligence items.

No source keyword, star, or engagement gate runs before Stage A. Provider calls
run outside SQLAlchemy sessions; all database writes remain serial in this job.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.ai.client import IntelTriageClient
from app.ai.skills.intel_triage import (
    AnalysisResult,
    RawIntelEnvelope,
    ScreenResult,
    analysis_guard_failure,
    apply_analysis_guards,
    apply_screen_guard,
    strict_parse_analysis,
    strict_parse_screen,
)
from app.config.limits import (
    DEFAULT_AI_ANALYSIS_MIN_SCORE,
    DEFAULT_AI_REVIEW_CONCURRENCY,
    DEFAULT_AI_REVIEW_LIMIT,
    DEFAULT_AI_SCREEN_REJECT_THRESHOLD,
)
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, load_source_registry
from app.domain.models import SourceSpec
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem, IntelRun
from app.storage.repository import IntelRepository
from app.jobs.stage_a_screen_job import run_stage_a_screen_job
from app.jobs.stage_b_analysis_job import run_stage_b_analysis_job

LOGGER = logging.getLogger(__name__)


@dataclass
class AIReviewResult:
    run_id: int | None = None
    processed: int = 0
    screened: int = 0
    screened_out: int = 0
    screen_failed: int = 0
    analyzed: int = 0
    analysis_filtered: int = 0
    analysis_failed: int = 0
    candidate: int = 0
    candidate_ids: list[int] = field(default_factory=list)
    partial: bool = False
    partial_reason: str | None = None
    ai_limit: int | None = None
    errors: list[str] = field(default_factory=list)
    candidate_path: str = "output/ai-review/ai_review_candidates.jsonl"
    audit_path: str = "output/ai-review/ai_review_audit.jsonl"
    markdown_path: str = "output/ai-review/ai_review_digest.md"
    exported: int = 0
    audit_exported: int = 0
    dry_run: bool = False

    @property
    def selected(self) -> int:
        return self.candidate

    @property
    def filtered(self) -> int:
        return self.screened_out + self.analysis_filtered

    @property
    def ai_failed(self) -> int:
        return self.screen_failed + self.analysis_failed

    @property
    def failed(self) -> int:
        return self.ai_failed

    @property
    def run_counts(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "screened": self.screened,
            "screen_count": self.screened,
            "screened_out": self.screened_out,
            "screen_failed": self.screen_failed,
            "analyzed": self.analyzed,
            "analyze_count": self.analyzed,
            "analysis_filtered": self.analysis_filtered,
            "analysis_failed": self.analysis_failed,
            "candidate": self.candidate,
            "filtered_count": self.filtered,
            "failure_count": self.failed,
            "partial": self.partial,
            "partial_reason": self.partial_reason,
        }


def run_ai_review_job(
    *,
    session_factory: sessionmaker[Session],
    source_specs: Mapping[str, SourceSpec] | None = None,
    ai_client: IntelTriageClient | Any | None = None,
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    ai_limit: int | None = None,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    output_dir: str | Path = "output/ai-review",
    http_client: Any | None = None,
    now: Any | None = None,
    run_id: int | None = None,
    screen_reject_threshold: int = DEFAULT_AI_SCREEN_REJECT_THRESHOLD,
    analysis_min_score: int = DEFAULT_AI_ANALYSIS_MIN_SCORE,
    concurrency: int = DEFAULT_AI_REVIEW_CONCURRENCY,
) -> AIReviewResult:
    """Run structural prefilter, Stage A screen, then Stage B analysis.

    A supplied limit is an explicit safety cap. The default None processes the
    whole selected scope; capped runs remain auditable as partial.
    """
    del http_client, now
    explicit_cap = ai_limit is not None or limit is not None
    if ai_limit is not None:
        limit = ai_limit
    limit = _normalise_limit(limit)
    result = AIReviewResult(run_id=run_id, dry_run=dry_run, ai_limit=limit)
    output_path = Path(output_dir)
    result.candidate_path = str(output_path / "ai_review_candidates.jsonl")
    result.audit_path = str(output_path / "ai_review_audit.jsonl")
    result.markdown_path = str(output_path / "ai_review_digest.md")
    specs = dict(source_specs or {})

    # Keep dry-run side-effect free: no run, stage rows, projection rows, or
    # provider calls are created.
    if dry_run:
        with session_factory() as session:
            items = IntelRepository(session).list_pending_items(
                limit=None,
                source_id=source_filter,
                content_class=content_class,
                force=force,
                run_id=run_id,
                stage="screen",
            )
            if limit is not None:
                items = items[:limit]
            result.processed = len(items)
            result.partial = explicit_cap
            result.partial_reason = f"ai_limit:{limit}" if explicit_cap else None
        return result

    stage_a = run_stage_a_screen_job(
        session_factory=session_factory,
        source_specs=specs,
        ai_client=ai_client,
        run_id=run_id,
        limit=limit,
        source_filter=source_filter,
        content_class=content_class,
        force=force,
        screen_reject_threshold=screen_reject_threshold,
        concurrency=concurrency,
    )
    effective_run_id = stage_a.run_id
    stage_b = run_stage_b_analysis_job(
        session_factory=session_factory,
        source_specs=specs,
        ai_client=ai_client,
        run_id=effective_run_id,
        limit=limit,
        source_filter=source_filter,
        content_class=content_class,
        force=force,
        item_ids=stage_a.item_ids,
        analysis_min_score=analysis_min_score,
        concurrency=concurrency,
    )
    result.run_id = effective_run_id
    result.processed = stage_a.processed
    result.screened = stage_a.screened
    result.screened_out = stage_a.screened_out
    result.screen_failed = stage_a.screen_failed
    result.analyzed = stage_b.analyzed
    result.analysis_filtered = stage_b.analysis_filtered
    result.analysis_failed = stage_b.analysis_failed
    result.candidate = stage_b.candidate
    result.candidate_ids = list(stage_b.candidate_ids)
    result.partial = bool(stage_a.partial or stage_b.partial)
    result.partial_reason = stage_a.partial_reason or stage_b.partial_reason
    result.errors = [*stage_a.errors, *stage_b.errors]

    if effective_run_id is not None:
        _persist_run_counts(session_factory, effective_run_id, result)

    with session_factory() as session:
        candidates, audit = _load_stage_exports(
            session,
            run_id=effective_run_id,
            source_filter=source_filter,
            content_class=content_class,
        )
    result.exported = len(candidates)
    result.audit_exported = len(audit)
    _write_stage_exports(candidates, audit, output_dir=output_path, result=result)
    return result


def run_ai_review_from_settings(
    *,
    settings: Settings,
    registry_path=DEFAULT_REGISTRY_PATH,
    output_dir: str | Path = "output/ai-review",
    limit: int | None = DEFAULT_AI_REVIEW_LIMIT,
    ai_limit: int | None = None,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    http_client: Any | None = None,
    ai_client: Any | None = None,
    run_id: int | None = None,
) -> AIReviewResult:
    registry = load_source_registry(registry_path, env={"RSSHUB_BASE_URL": settings.rsshub_base_url or ""})
    specs = {source.id: source for source in registry.sources}
    database_url = _readable_database_url(settings.database_url, dry_run=dry_run)
    engine = create_engine_from_url(database_url)
    if not dry_run or database_url == "sqlite:///:memory:":
        init_db(engine)

    own_client = http_client is None and ai_client is None
    client = http_client or (
        httpx.Client(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            http2=True,
            trust_env=True,
            headers={"User-Agent": settings.user_agent},
        )
        if ai_client is None
        else None
    )
    try:
        provider = ai_client or IntelTriageClient.from_settings(settings, http_client=client)
        return run_ai_review_job(
            session_factory=create_session_factory(engine),
            source_specs=specs,
            ai_client=provider,
            limit=limit,
            ai_limit=ai_limit,
            source_filter=source_filter,
            content_class=content_class,
            force=force,
            dry_run=dry_run,
            output_dir=output_dir,
            run_id=run_id,
            screen_reject_threshold=settings.ai_screen_reject_threshold,
            analysis_min_score=settings.ai_analysis_min_score,
            concurrency=settings.ai_review_concurrency,
        )
    finally:
        if own_client and client is not None:
            client.close()


def _parallel_map(entries: Sequence[Any], func: Any, *, max_workers: int) -> list[Any]:
    if not entries:
        return []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="intel-ai") as executor:
        futures = [executor.submit(func, entry) for entry in entries]
        return [future.result() for future in futures]


def _screen_one(client: Any, envelope: RawIntelEnvelope, reject_threshold: int) -> ScreenResult:
    try:
        method = getattr(client, "screen", None)
        if not callable(method):
            raise TypeError("AI client does not expose screen")
        value = method(envelope)
        if isinstance(value, ScreenResult):
            parsed = value.with_item(envelope)
        else:
            parsed = strict_parse_screen(value, envelope=envelope, reject_threshold=reject_threshold)
        return apply_screen_guard(parsed.with_item(envelope), envelope, reject_threshold=reject_threshold)
    except Exception as exc:
        return ScreenResult(
            item_id=envelope.item_id,
            decision="uncertain",
            reason_code="provider_failure",
            reason="Stage A provider call failed",
            confidence=0,
            risk_flags=["ai:screen_failed"],
            status="screen_failed",
            error_code=exc.__class__.__name__,
            error_message=str(exc)[:4000] or exc.__class__.__name__,
            raw_response=None,
        )


def _analysis_one(client: Any, envelope: RawIntelEnvelope) -> AnalysisResult:
    try:
        method = getattr(client, "analyze", None)
        if not callable(method):
            raise TypeError("AI client does not expose analyze")
        value = method(envelope)
        if isinstance(value, AnalysisResult):
            parsed = value.with_item(envelope)
        else:
            parsed = strict_parse_analysis(value, envelope=envelope)
        return apply_analysis_guards(parsed.with_item(envelope), envelope)
    except Exception as exc:
        return AnalysisResult(
            item_id=envelope.item_id,
            topic="opinion",
            topics=["opinion"],
            summary_cn="",
            keywords=[],
            entities=[],
            selection_score=0,
            score_components={},
            paper_support={"is_paper": False},
            risk_flags=["ai:analysis_failed"],
            reason="Stage B provider call failed",
            confidence=0,
            source_content_class=envelope.source_content_class,
            source_group=envelope.source_group,
            status="analysis_failed",
            error_code=exc.__class__.__name__,
            error_message=str(exc)[:4000] or exc.__class__.__name__,
            raw_response=None,
        )


def _structurally_valid(item: IntelItem) -> bool:
    return bool(
        str(item.source_id or "").strip()
        and str(item.title or "").strip()
        and str(item.content_hash or "").strip()
    )


def _structural_screen(item: IntelItem, *, error: str | None = None) -> ScreenResult:
    return ScreenResult(
        item_id=item.id,
        decision="reject",
        reason_code="structural_invalid",
        reason=error[:4000] if error else "item failed the structural prefilter",
        confidence=100,
        risk_flags=["prefilter:structural_invalid"],
        raw_response={"prefilter": "structural_invalid", "error": error} if error else {"prefilter": "structural_invalid"},
    )


def _item_to_envelope(item: IntelItem, spec: SourceSpec) -> RawIntelEnvelope:
    source = item.source
    return RawIntelEnvelope(
        item_id=item.id,
        source_id=item.source_id,
        source_name=source.name if source is not None else spec.name,
        source_group=spec.source_group or (source.source_group if source is not None else None),
        source_subtype=spec.source_subtype or (source.source_subtype if source is not None else None),
        source_role=spec.source_role or (source.source_role if source is not None else None),
        source_tier=spec.tier or (source.tier if source is not None else None),
        source_content_class=spec.content_class or item.content_class or "community_social",
        external_id=item.external_id,
        content_hash=item.content_hash,
        title=item.title,
        url=item.canonical_url,
        published_at=item.published_at,
        captured_at=item.captured_at,
        summary=item.summary,
        body_text=item.content_text or item.summary,
        metrics=_json_dict(item.metrics_json),
        raw_payload=_json_dict(item.raw_payload_json),
    )


def _spec_from_row(row: Any) -> SourceSpec:
    if row is None:
        return SourceSpec.model_validate(
            {"id": "unknown", "name": "unknown", "transport": "feed", "url": "https://invalid.local/", "content_class": "community_social"}
        )
    data: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "transport": row.transport,
        "url": row.url,
        "enabled": row.enabled,
        "priority": row.priority,
        "fetch_interval": row.fetch_interval,
        "default_limit": row.default_limit,
        "source_group": row.source_group,
        "source_subtype": row.source_subtype,
        "source_role": row.source_role,
        "spam_risk": row.spam_risk,
        "quality_weight": row.quality_weight,
        "content_class": row.content_class,
        "selection_policy": _json_dict(row.selection_policy_json),
    }
    if row.transport in {"feed", "rsshub"}:
        data["feed"] = {"format": row.feed_format or "rss", "adapter": row.feed_adapter or "generic"}
    elif row.transport == "github":
        github: dict[str, Any] = {"mode": row.github_mode or "search"}
        for name in ("query", "sort", "order", "pushed_days", "period"):
            value = getattr(row, f"github_{name}", None)
            if value is not None:
                github[name] = value
        data["github"] = github
    return SourceSpec.model_validate(data)


def _load_stage_exports(
    session: Session,
    *,
    run_id: int | None,
    source_filter: str | None,
    content_class: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repo = IntelRepository(session)
    if run_id is not None:
        items = repo.list_run_items(run_id, role="fetched")
    else:
        items = repo.list_pending_items(limit=None, force=True, stage="screen")
    if source_filter:
        items = [item for item in items if item.source_id == source_filter]
    if content_class:
        items = [item for item in items if item.content_class == content_class]
    records = [_serialize_stage_item(item) for item in items]
    return [record for record in records if record.get("status") == "candidate"], records


def _serialize_stage_item(item: IntelItem) -> dict[str, Any]:
    source = item.source
    screen = item.ai_screen
    review = item.ai_review
    source_group = source.source_group if source else None
    source_ref = {
        "id": item.source_id,
        "name": source.name if source else None,
        "transport": source.transport if source else None,
        "source_group": source_group,
        "source_subtype": source.source_subtype if source else None,
        "tier": source.tier if source else None,
        "role": source.source_role if source else None,
        "x_official": source_group == "x_official",
    }
    screen_record = None
    if screen is not None:
        screen_record = {
            "decision": screen.decision,
            "reason_code": screen.reason_code,
            "reason": screen.reason,
            "confidence": screen.confidence,
            "risk_flags": screen.risk_flags,
            "status": screen.status,
            "error_message": screen.error_message,
            "raw_response": screen.raw_response,
        }
    analysis_record = None
    if review is not None:
        analysis_record = {
            "topic": review.topic,
            "topics": review.topics,
            "summary_cn": review.summary_cn,
            "keywords": review.keywords,
            "entities": review.entities,
            "selection_score": review.selection_score,
            "score_components": review.score_components,
            "paper_support": review.paper_support,
            "risk_flags": review.risk_flags,
            "reason": review.reason,
            "confidence": review.confidence,
            "status": review.status,
            "error_message": review.error_message,
            "raw_response": review.raw_response,
        }
    return {
        "record_type": "ai_review_candidate" if item.status == "candidate" else "ai_review_audit",
        "stage": "ai_review",
        "id": item.id,
        "item_id": item.id,
        "source_id": item.source_id,
        "source": source_ref,
        "source_name": source.name if source else None,
        "source_group": source_group,
        "source_subtype": source.source_subtype if source else None,
        "content_class": item.content_class,
        "status": item.status,
        "title": item.title,
        "url": item.canonical_url,
        "summary": item.summary,
        "summary_cn": review.summary_cn if review and review.summary_cn else item.summary,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "captured_at": item.captured_at.isoformat() if item.captured_at else None,
        "selection_score": item.selection_score,
        "selection_reason": item.selection_reason,
        "metrics": _json_dict(item.metrics_json),
        "screen": screen_record,
        "analysis": analysis_record,
        "ai": analysis_record,
    }


def _write_stage_exports(
    candidates: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    *,
    output_dir: Path,
    result: AIReviewResult,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for record in [*candidates, *audit]:
        record["run_partial"] = bool(result.partial)
        record["run_partial_reason"] = result.partial_reason
        record["candidate_ids"] = list(result.candidate_ids)
        record["run_counts"] = result.run_counts
    (output_dir / "ai_review_candidates.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in candidates),
        encoding="utf-8",
    )
    (output_dir / "ai_review_audit.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in audit),
        encoding="utf-8",
    )
    lines = [
        "# AI Stage A/B 审计",
        "",
        f"候选条目：{len(candidates)}",
        f"审计条目：{len(audit)}",
        f"候选 IDs：{','.join(str(item_id) for item_id in result.candidate_ids) or '无'}",
        f"运行计数：{json.dumps(result.run_counts, ensure_ascii=False)}",
        "",
    ]
    for index, record in enumerate(candidates, 1):
        analysis = record.get("analysis") or {}
        lines.extend(
            [
                f"## {index}. {record.get('title') or '(untitled)'}",
                f"- 状态：{record.get('status')} | score={analysis.get('selection_score', 0)} | topic={analysis.get('topic') or '-'}",
                f"- 来源：{record.get('source_id')} / {record.get('source_group') or '-'}",
                f"- 摘要：{analysis.get('summary_cn') or record.get('summary') or '暂无摘要'}",
                f"- 风险：{', '.join(analysis.get('risk_flags') or []) or '无'}",
                f"- 链接：{record.get('url') or '无'}",
                "",
            ]
        )
    if result.partial:
        lines.extend([f"> 本次运行是 partial：{result.partial_reason or 'explicit ai limit'}", ""])
    (output_dir / "ai_review_digest.md").write_text("\n".join(lines), encoding="utf-8")


def _persist_run_counts(session_factory: sessionmaker[Session], run_id: int, result: AIReviewResult) -> None:
    with session_factory() as session:
        run = session.get(IntelRun, int(run_id))
        if run is None:
            return
        run.screened = result.screened
        run.screened_out = result.screened_out
        run.screen_failed = result.screen_failed
        run.analyzed = result.analyzed
        run.analysis_filtered = result.analysis_filtered
        run.analysis_failed = result.analysis_failed
        run.candidate = result.candidate
        run.selected = result.candidate
        run.partial = bool(result.partial)
        run.partial_reason = result.partial_reason
        run.failed = result.failed
        session.commit()


def _bounded_score(value: Any, default: int) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def _normalise_limit(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _readable_database_url(database_url: str, *, dry_run: bool) -> str:
    if not dry_run or not database_url.startswith("sqlite:///"):
        return database_url
    path_text = database_url[len("sqlite:///") :]
    if path_text in {":memory:", ""}:
        return "sqlite:///:memory:"
    path = Path(path_text)
    if not path.is_absolute():
        path = Path.cwd() / path
    return database_url if path.exists() else "sqlite:///:memory:"


__all__ = ["AIReviewResult", "run_ai_review_job", "run_ai_review_from_settings"]
