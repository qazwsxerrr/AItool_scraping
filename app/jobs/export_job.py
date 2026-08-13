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
from app.github.report import write_github_trending_report
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
    github_report_path: str | None = None
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
    github_report_dir: str | Path | None = None,
) -> IntelExportResult:
    output = Path(output_dir)
    jsonl_path = output / "intel_items.jsonl"
    markdown_path = output / "intel_digest.md"
    pending_path = output / "intel_pending.jsonl"
    report_root = Path(github_report_dir) if github_report_dir else output.parent / "github-trending"
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

    github_report_path: Path | None = None
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
        github_report_path = write_github_trending_report(records, output_root=report_root)

    return IntelExportResult(
        exported=len(records),
        pending=len(pending_records),
        jsonl_path=str(jsonl_path),
        markdown_path=str(markdown_path),
        pending_path=str(pending_path),
        dry_run=dry_run,
        github_report_path=str(github_report_path) if github_report_path else None,
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
    github_report_dir: str | Path | None = None,
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
        github_report_dir=github_report_dir,
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
            | ((IntelItem.status == "hotspot") & (AIItemReview.status == "ai_failed"))
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
    source = item.source
    source_group = source.source_group if source else None
    source_subtype = source.source_subtype if source else None
    source_transport = source.transport if source else None
    source_tier = source.tier if source else None
    source_role = source.source_role if source else None
    source_ref = {
        "id": item.source_id,
        "name": source.name if source else None,
        "source_id": item.source_id,
        "transport": source_transport,
        "source_group": source_group,
        "source_subtype": source_subtype,
        "tier": source_tier,
        "role": source_role,
        "x_official": source_group == "x_official",
    }
    return {
        "id": item.id,
        "source_id": item.source_id,
        "name": source.name if source else None,
        "source_name": source.name if source else None,
        "source_transport": source_transport,
        # Keep the historical export key as a transport-valued alias for
        # readers consuming existing JSONL files. Routing no longer depends on
        # this compatibility field; new code uses ``source_transport``.
        "source_type": source_transport,
        "transport": source_transport,
        "source_group": source_group,
        "source_subtype": source_subtype,
        "tier": source_tier,
        "source_tier": source_tier,
        "source_role": source_role,
        "role": source_role,
        "x_official": source_group == "x_official",
        "source": source_ref,
        "source_priority": source.priority if source else None,
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
                (
                    f"- 来源标识：`{record.get('source_id')}` | transport=`{record.get('source_transport')}` "
                    f"group=`{record.get('source_group')}` subtype=`{record.get('source_subtype')}` "
                    f"tier=`{record.get('tier')}` role=`{record.get('source_role')}` "
                    f"x_official=`{str(bool(record.get('x_official'))).lower()}`"
                ),
                f"- 摘要：{ai.get('summary_cn') or record.get('summary') or '暂无摘要'}",
                f"- 核实：`{verification.get('status') if verification else '未执行'}` / `{verification.get('mode') if verification else 'n/a'}`",
                f"- 风险：{', '.join(ai.get('risk_flags') or verification.get('risk_flags') or []) or '无'}",
                f"- 链接：{record.get('url') or '无'}",
                "",
            ]
        )
        if _is_github_repository(record):
            metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
            periods = _trending_periods(metrics)
            weekly = periods.get("weekly", {})
            daily = periods.get("daily", {})
            topics = _string_list(metrics.get("topics")) + _string_list(metrics.get("search_topics"))
            lines.extend(
                [
                    "### GitHub 项目指标",
                    f"- 累计 Star：{_count(metrics.get('stars'))}",
                    f"- 本周新增 Star：{_count(weekly.get('stars_since')) if weekly else '-'}",
                    f"- 今日新增 Star：{_count(daily.get('stars_since')) if daily else '-'}",
                    f"- Fork：{_count(metrics.get('forks'))}",
                    f"- Topics：{', '.join(topics) or '暂无'}",
                    f"- 项目介绍：{ai.get('summary_cn') or record.get('summary') or '暂无介绍'}",
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


def _is_github_repository(record: dict[str, Any]) -> bool:
    source_transport = str(record.get("source_transport") or "").casefold()
    source_type = str(record.get("source_type") or "").casefold()
    source_id = str(record.get("source_id") or "").casefold()
    external_id = str(record.get("external_id") or "").casefold()
    url = str(record.get("url") or "").casefold()
    payload = record.get("raw_payload")
    payload_type = payload.get("github_item_type") if isinstance(payload, dict) else None
    return (
        record.get("content_class") == "project_tool"
        and payload_type not in {"release"}
        and not external_id.startswith("github_release:")
        and (
            payload_type == "repository"
            or source_transport == "github"
            or source_type in {"github", "github_api", "github_trending"}
            or source_id.startswith("github_")
            or external_id.startswith("github_repo:")
            or "github.com/" in url
        )
    )


def _trending_periods(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = metrics.get("trending")
    periods = {key: dict(value) for key, value in raw.items() if isinstance(value, dict)} if isinstance(raw, dict) else {}
    period = metrics.get("trending_period")
    if period and period not in periods:
        periods[str(period)] = {
            "rank": metrics.get("trending_rank"),
            "stars_since": metrics.get("stars_since"),
            "stars": metrics.get("stars"),
            "forks": metrics.get("forks"),
        }
    return periods


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(entry) for entry in value if str(entry).strip()]


def _count(value: Any) -> str:
    try:
        number = int(float(str(value).replace(",", ""))) if value is not None else 0
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}" if number else "-"
