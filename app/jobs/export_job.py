"""Pure read/export stage for v2 intelligence records."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import IntelRepository
from app.storage.models import AIItemReview, IntelItem, IntelItemVerification


@dataclass(frozen=True)
class IntelExportResult:
    exported: int
    pending: int
    jsonl_path: str
    markdown_path: str
    pending_path: str
    dry_run: bool = False
    status_counts: dict[str, int] = field(default_factory=dict)
    failure_counts: dict[str, int] = field(default_factory=dict)


def run_intel_export_job(
    *,
    session_factory: sessionmaker[Session],
    output_dir: str | Path = "output/intel",
    limit: int | None = 100,
    source_filter: str | None = None,
    content_class: str | None = None,
    dry_run: bool = False,
) -> IntelExportResult:
    output = Path(output_dir)
    jsonl_path = output / "intel_items.jsonl"
    markdown_path = output / "intel_digest.md"
    pending_path = output / "intel_pending.jsonl"
    with session_factory() as session:
        repo = IntelRepository(session)
        items = repo.list_export_items(
            limit=limit,
            source_id=source_filter,
            content_class=content_class,
        )
        pending_items = _list_pending(
            session,
            limit=limit,
            source_filter=source_filter,
            content_class=content_class,
        )
        records = [_serialize(item) for item in items]
        pending_records = [_serialize(item) for item in pending_items]

    status_counts = dict(Counter(str(record.get("status") or "unknown") for record in [*records, *pending_records]))
    failure_counts = {
        status: count
        for status, count in status_counts.items()
        if status in {"failed", "ai_failed", "needs_review"}
    }

    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        jsonl_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in records),
            encoding="utf-8",
        )
        pending_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in pending_records),
            encoding="utf-8",
        )
        markdown_path.write_text(
            _markdown(records, pending_records, status_counts=status_counts, failure_counts=failure_counts),
            encoding="utf-8",
        )

    return IntelExportResult(
        exported=len(records),
        pending=len(pending_records),
        jsonl_path=str(jsonl_path),
        markdown_path=str(markdown_path),
        pending_path=str(pending_path),
        dry_run=dry_run,
        status_counts=status_counts,
        failure_counts=failure_counts,
    )


def run_intel_export_from_settings(
    *,
    settings: Settings,
    output_dir: str | Path = "output/intel",
    limit: int | None = 100,
    source_filter: str | None = None,
    content_class: str | None = None,
    dry_run: bool = False,
) -> IntelExportResult:
    database_url = _readable_database_url(settings.database_url, dry_run=dry_run)
    engine = create_engine_from_url(database_url)
    if not dry_run or database_url == "sqlite:///:memory:":
        init_db(engine)
    return run_intel_export_job(
        session_factory=create_session_factory(engine),
        output_dir=output_dir,
        limit=limit,
        source_filter=source_filter,
        content_class=content_class,
        dry_run=dry_run,
    )


def _list_pending(
    session: Session,
    *,
    limit: int | None,
    source_filter: str | None,
    content_class: str | None,
) -> list[IntelItem]:
    # Pending includes items needing official confirmation and per-item AI
    # failures. It is intentionally separate from the strong export set.
    stmt = (
        select(IntelItem)
        .options(joinedload(IntelItem.source), joinedload(IntelItem.ai_review), joinedload(IntelItem.verification))
        .outerjoin(AIItemReview, AIItemReview.item_id == IntelItem.id)
        .where(
            (IntelItem.status.in_(["needs_review", "ai_failed"]))
            | ((IntelItem.status == "selected") & AIItemReview.id.is_(None))
        )
        .order_by(IntelItem.selection_score.desc(), IntelItem.id.asc())
    )
    if source_filter:
        stmt = stmt.where(IntelItem.source_id == source_filter)
    if content_class:
        stmt = stmt.where(IntelItem.content_class == content_class)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).unique().all())


def _serialize(item: IntelItem) -> dict[str, Any]:
    review = item.ai_review
    verification = item.verification
    return {
        "id": item.id,
        "source_id": item.source_id,
        "source_name": item.source.name if item.source else None,
        "source_type": item.source.type if item.source else None,
        "source_priority": item.source.priority if item.source else None,
        "external_id": item.external_id,
        "content_hash": item.content_hash,
        "content_class": item.content_class,
        "status": item.status,
        "title": item.title,
        "url": item.canonical_url,
        "summary": item.summary,
        "content_text": item.content_text,
        "published_at": _date(item.published_at),
        "captured_at": _date(item.captured_at),
        "selection_score": item.selection_score,
        "selection_reason": item.selection_reason,
        "metrics": _json(item.metrics_json, {}),
        "raw_payload": _json(item.raw_payload_json, {}),
        "discovered_links": _json(item.discovered_links_json, []),
        "ai": {
            "model": review.model,
            "status": review.status,
            "keep": review.keep,
            "content_class": review.content_class,
            "summary_cn": review.summary_cn,
            "reason": review.reason,
            "risk_flags": _json(review.risk_flags_json, []),
            "needs_verification": review.needs_verification,
            "official_url": review.official_url,
            "confidence": review.confidence,
            "error_message": review.error_message,
        }
        if review
        else None,
        "verification": {
            "mode": verification.mode,
            "status": verification.status,
            "verification_url": verification.verification_url,
            "source_domain": verification.source_domain,
            "http_status": verification.http_status,
            "title": verification.title,
            "content_preview": verification.content_preview,
            "supports_basic_fact": verification.supports_basic_fact,
            "risk_flags": _json(verification.risk_flags_json, []),
            "reason": verification.reason,
            "checked_at": _date(verification.checked_at),
        }
        if verification
        else None,
    }


def _markdown(
    records: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    *,
    status_counts: dict[str, int] | None = None,
    failure_counts: dict[str, int] | None = None,
) -> str:
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("content_class") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    lines = ["# AI 情报日报", "", f"保留条目：{len(records)}", f"待处理/待核实：{len(pending)}", ""]
    if counts:
        lines.append("分类统计：" + "、".join(f"{key}={value}" for key, value in sorted(counts.items())))
        lines.append("")
    if status_counts:
        lines.append("状态统计：" + "、".join(f"{key}={value}" for key, value in sorted(status_counts.items())))
        lines.append("失败/待核实统计：" + ("、".join(f"{key}={value}" for key, value in sorted((failure_counts or {}).items())) or "无"))
        lines.append("")
    for index, record in enumerate(records, start=1):
        ai = record.get("ai") or {}
        verification = record.get("verification") or {}
        lines.extend(
            [
                f"## {index}. {record.get('title') or '(untitled)'}",
                f"- 类别：`{record.get('content_class')}` | 状态：`{record.get('status')}` | 选择分：`{record.get('selection_score')}`",
                f"- 来源：`{record.get('source_name') or record.get('source_id')}`",
                f"- 摘要：{ai.get('summary_cn') or record.get('summary') or '暂无摘要'}",
                f"- 核实：`{verification.get('status') if verification else '未执行'}` / `{verification.get('mode') if verification else 'n/a'}`",
                f"- 风险：{', '.join(ai.get('risk_flags') or verification.get('risk_flags') or []) or '无'}",
                f"- 链接：{record.get('url') or '无'}",
                "",
            ]
        )
    if pending:
        lines.extend(["## 待核实", ""])
        for record in pending:
            lines.append(f"- `{record.get('status')}` {record.get('title')}：{record.get('url') or '无链接'}")
    return "\n".join(lines) + "\n"


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _date(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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
