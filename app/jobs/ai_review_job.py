"""AI-only editorial pre-processing for fetched intelligence items.

This stage deliberately stops after deterministic selection and one structured
AI call per retained item.  It does not invoke the lightweight verifier,
evidence search/fetch, claims, or any later editorial job.  The output is a
candidate digest plus an audit file containing filtered and failed rows.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.ai.client import ItemAnalysisClient
from app.ai.schemas import ItemAnalysisRequest, ItemAnalysisResponse
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, load_source_registry
from app.domain.models import SourceSpec
from app.domain.policies import selection_decision
from app.jobs.export_job import _serialize as _serialize_intel_item
from app.jobs.process_job import (
    _coerce_analysis_response,
    _coerce_github_project_response,
    _github_project_summary_request,
    _is_github_repository_item,
    _is_github_source,
    _item_to_fetch_item,
    _latest_item_time,
    _ranking_key,
    _selection_reason,
    _spec_from_row,
)
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem
from app.storage.repository import IntelRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class AIReviewResult:
    processed: int = 0
    selected: int = 0
    filtered: int = 0
    analyzed: int = 0
    ai_failed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    candidate_path: str = "output/ai-review/ai_review_candidates.jsonl"
    audit_path: str = "output/ai-review/ai_review_audit.jsonl"
    markdown_path: str = "output/ai-review/ai_review_digest.md"
    exported: int = 0
    audit_exported: int = 0
    dry_run: bool = False


def run_ai_review_job(
    *,
    session_factory: sessionmaker[Session],
    source_specs: Mapping[str, SourceSpec] | None = None,
    ai_client: ItemAnalysisClient | Any | None = None,
    limit: int | None = 100,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    output_dir: str | Path = "output/ai-review",
    http_client: Any | None = None,
    now: datetime | None = None,
) -> AIReviewResult:
    """Run deterministic filtering plus structured AI review, without verify.

    ``ai_client`` may use an injected HTTP client for its provider request.
    The client is never used for source/article verification here.  In
    particular, GitHub projects are summarized from already-persisted fetch
    material and do not trigger metadata enrichment HTTP.
    """

    # Accepted for migration compatibility but deliberately unused: this
    # stage has no source/article verification HTTP boundary.
    del http_client
    result = AIReviewResult(dry_run=dry_run)
    result.candidate_path = str(Path(output_dir) / "ai_review_candidates.jsonl")
    result.audit_path = str(Path(output_dir) / "ai_review_audit.jsonl")
    result.markdown_path = str(Path(output_dir) / "ai_review_digest.md")
    specs = dict(source_specs or {})

    with session_factory() as session:
        repo = IntelRepository(session)
        items = _list_ai_review_items(
            session,
            limit=None,
            source_filter=source_filter,
            content_class=content_class,
            force=force,
        )
        stage_now = now or _latest_item_time(items) or datetime.now(timezone.utc)
        ranked: list[tuple[IntelItem, Any, SourceSpec]] = []
        for item in items:
            spec = specs.get(item.source_id) or _spec_from_row(item.source)
            decision = selection_decision(_item_to_fetch_item(item), spec, now=stage_now)
            ranked.append((item, decision, spec))
        ranked.sort(key=_ranking_key, reverse=True)
        if limit is not None:
            ranked = ranked[:limit]

        for item, decision, spec in ranked:
            result.processed += 1
            try:
                if dry_run:
                    if decision.selected:
                        result.selected += 1
                    else:
                        result.filtered += 1
                    continue

                repo.save_selection(
                    item.id,
                    keep=decision.selected,
                    score=0 if _is_github_source(spec) else round(decision.score),
                    reason=_selection_reason(decision),
                )
                session.commit()
                if not decision.selected:
                    result.filtered += 1
                    continue
                result.selected += 1

                if ai_client is None:
                    raise RuntimeError("item analysis client is not configured")
                response = _run_item_ai_review(item, spec, ai_client, decision)
                if not response.summary_cn.strip() and response.keep:
                    raise ValueError("AI review returned an empty summary_cn")
                repo.upsert_ai_review(
                    item.id,
                    response,
                    model=getattr(ai_client, "model", None),
                    content_class=spec.content_class,
                )
                # This is intentionally the only durable item status for a
                # retained AI candidate. Verification remains untouched.
                repo.set_item_status(item.id, "selected" if response.keep else "rejected")
                result.analyzed += 1
                session.commit()
            except Exception as exc:
                result.failed += 1
                result.ai_failed += 1
                message = f"intel_item_id={item.id}: {exc}"
                result.errors.append(message)
                LOGGER.exception("AI review failed for intel item %s", item.id)
                if dry_run:
                    continue
                try:
                    session.rollback()
                    repo.upsert_ai_review(
                        item.id,
                        None,
                        model=getattr(ai_client, "model", None) if ai_client is not None else None,
                        content_class=spec.content_class,
                        status="ai_failed",
                        error_message=str(exc),
                    )
                    repo.set_item_status(item.id, "ai_failed")
                    session.commit()
                except Exception:
                    session.rollback()
                    LOGGER.exception("Failed to persist AI review error for intel item %s", item.id)

        if not dry_run:
            # The processing loop loaded relationships before writing the
            # review rows. Expire the identity map so the export query sees
            # the just-persisted AI result instead of a stale ``ai_review=None``.
            session.expire_all()
            candidates, audit = _load_ai_review_exports(
                session,
                source_filter=source_filter,
                content_class=content_class,
            )
        else:
            candidates, audit = [], []

    result.exported = len(candidates)
    result.audit_exported = len(audit)
    if not dry_run:
        _write_ai_review_exports(candidates, audit, output_dir=output_dir)
    return result


def run_ai_review_from_settings(
    *,
    settings: Settings,
    registry_path=DEFAULT_REGISTRY_PATH,
    output_dir: str | Path = "output/ai-review",
    limit: int | None = 100,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    http_client: Any | None = None,
) -> AIReviewResult:
    registry = load_source_registry(registry_path, env={"RSSHUB_BASE_URL": settings.rsshub_base_url or ""})
    specs = {source.id: source for source in registry.sources}
    database_url = _readable_database_url(settings.database_url, dry_run=dry_run)
    engine = create_engine_from_url(database_url)
    if not dry_run or database_url == "sqlite:///:memory:":
        init_db(engine)
    own_client = http_client is None
    client = http_client or httpx.Client(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        http2=True,
        trust_env=True,
        headers={"User-Agent": settings.user_agent},
    )
    try:
        ai_client = ItemAnalysisClient.from_settings(settings, http_client=client)
        return run_ai_review_job(
            session_factory=create_session_factory(engine),
            source_specs=specs,
            ai_client=ai_client,
            limit=limit,
            source_filter=source_filter,
            content_class=content_class,
            force=force,
            dry_run=dry_run,
            output_dir=output_dir,
        )
    finally:
        if own_client:
            client.close()


def _run_item_ai_review(
    item: IntelItem,
    spec: SourceSpec,
    ai_client: Any,
    decision: Any,
) -> ItemAnalysisResponse:
    if spec.content_class == "project_tool" and _is_github_repository_item(_item_to_fetch_item(item)):
        request = _github_project_summary_request(item)
        analyzer = getattr(ai_client, "summarize_project", None)
        if not callable(analyzer):
            analyzer = getattr(ai_client, "analyze", None)
        if not callable(analyzer):
            raise TypeError("AI client does not expose summarize_project/analyze")
        response = _coerce_github_project_response(analyzer(request), request)
        # GitHub retention is deterministic.  The summary call does not own
        # the keep decision, so a selected project is a kept candidate even
        # though the provider-neutral project-summary parser uses keep=False.
        return response.model_copy(
            update={
                "keep": True,
                "content_class": "project_tool",
                "needs_verification": False,
                "risk_flags": list(dict.fromkeys([*response.risk_flags, *[f"selection:{flag}" for flag in decision.risk_flags]])),
            }
        )

    fetch_item = _item_to_fetch_item(item)
    request = ItemAnalysisRequest(
        item_id=item.id,
        title=item.title,
        url=item.canonical_url,
        source_id=item.source_id,
        source_content_class=spec.content_class or "community_social",
        body_preview=(item.content_text or item.summary or "")[:8000],
        metrics=_json_dict(item.metrics_json),
    )
    response_value = ai_client.analyze(request)
    response = (
        response_value
        if isinstance(response_value, ItemAnalysisResponse)
        else _coerce_analysis_response(response_value, request.source_content_class)
    )
    extra_risks = [f"selection:{flag}" for flag in decision.risk_flags]
    return response.model_copy(
        update={
            "content_class": spec.content_class or response.content_class,
            "needs_verification": response.needs_verification or spec.content_class in {"official_model_company", "community_social"},
            "risk_flags": list(dict.fromkeys([*response.risk_flags, *extra_risks])),
        }
    )


def _list_ai_review_items(
    session: Session,
    *,
    limit: int | None,
    source_filter: str | None,
    content_class: str | None,
    force: bool,
) -> list[IntelItem]:
    stmt = (
        select(IntelItem)
        .options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review), joinedload(IntelItem.verification))
        .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc(), IntelItem.id.asc())
    )
    if not force:
        stmt = stmt.where(
            IntelItem.status.in_(["new", "selected", "ai_failed"])
            & ((~IntelItem.ai_review.has()) | (IntelItem.ai_review.has(AIItemReview.status == "ai_failed")))
        )
    if source_filter:
        stmt = stmt.where(IntelItem.source_id == source_filter)
    if content_class:
        stmt = stmt.where(IntelItem.content_class == content_class)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).unique().all())


def _load_ai_review_exports(
    session: Session,
    *,
    source_filter: str | None,
    content_class: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stmt = (
        select(IntelItem)
        .options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review), joinedload(IntelItem.verification))
        .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc(), IntelItem.id.asc())
    )
    if source_filter:
        stmt = stmt.where(IntelItem.source_id == source_filter)
    if content_class:
        stmt = stmt.where(IntelItem.content_class == content_class)
    items = list(session.scalars(stmt).unique().all())
    records = [_serialize_candidate(item) for item in items]
    candidates = [
        record
        for record in records
        if record.get("status") == "selected"
        and record["keep_decision"] is True
        and (record.get("ai") or {}).get("status") == "success"
    ]
    return candidates, records


def _serialize_candidate(item: IntelItem) -> dict[str, Any]:
    record = _serialize_intel_item(item)
    review = record.get("ai") or {}
    review = dict(review)
    review.update({"evidence_status": "not_run", "verification_status": "not_run"})
    record.update(
        {
            "record_type": "ai_review_candidate",
            "stage": "ai_review",
            "ai_review_stage": "ai_review",
            "keep_decision": bool(review.get("keep")) if review else False,
            "summary_cn": review.get("summary_cn") or item.summary,
            "ai_review_status": review.get("status") if review else "not_run",
            "ai": review or None,
            "evidence_status": "not_run",
            "verification_status": "not_run",
            # Never expose a prior verification row as the result of this
            # stage. It remains in the database for historical audit only.
            "verification": None,
        }
    )
    return record


def _write_ai_review_exports(
    candidates: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    *,
    output_dir: str | Path,
) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "ai_review_candidates.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in candidates),
        encoding="utf-8",
    )
    (output / "ai_review_audit.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in audit),
        encoding="utf-8",
    )
    lines = [
        "# AI Review 候选日报",
        "",
        f"候选条目：{len(candidates)}",
        f"审计条目：{len(audit)}",
        "",
        "> 本阶段只完成确定性初筛、AI 分类和中文简要总结；evidence/verification_status=not_run。",
        "",
    ]
    for index, record in enumerate(candidates, 1):
        ai = record.get("ai") or {}
        lines.extend(
            [
                f"## {index}. {record.get('title') or '(untitled)' }",
                f"- 来源：`{record.get('source_id')}` / `{record.get('source_group')}` / `{record.get('source_subtype')}` / x_official=`{str(bool(record.get('x_official'))).lower()}`",
                f"- 类别：`{record.get('content_class')}` | keep=`{str(bool(record.get('keep_decision'))).lower()}` | confidence=`{ai.get('confidence', 0)}`",
                f"- 摘要：{record.get('summary_cn') or '暂无摘要'}",
                f"- 风险：{', '.join(ai.get('risk_flags') or []) or '无'}",
                f"- evidence_status=`not_run` | verification_status=`not_run`",
                f"- 链接：{record.get('url') or '无'}",
                "",
            ]
        )
    if audit:
        lines.extend(["## 审计/待处理", ""])
        for record in audit:
            if record in candidates:
                continue
            ai = record.get("ai") or {}
            lines.append(
                f"- `{record.get('status')}` {record.get('title') or '(untitled)'}："
                f"ai_status=`{ai.get('status') if ai else 'not_run'}`，来源=`{record.get('source_id')}`"
            )
    (output / "ai_review_digest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


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
