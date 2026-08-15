"""Fetch-only orchestration and export for the first pipeline stage.

This module intentionally stops after collectors persist normalized
``intel_items``. It does not invoke AI review or export jobs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.config.limits import DEFAULT_FETCH_LIMIT_PER_SOURCE
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH, load_source_registry
from app.domain.models import SourceSpec
from app.jobs.fetch_job import IntelFetchResult, run_intel_fetch_from_settings, run_intel_fetch_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem, Source


@dataclass(frozen=True)
class FetchOnlyExportResult:
    exported: int
    json_path: str
    jsonl_path: str
    markdown_path: str
    dry_run: bool = False


@dataclass(frozen=True)
class FetchOnlyResult:
    fetch: IntelFetchResult
    export: FetchOnlyExportResult | None = None

    @property
    def exported(self) -> int:
        return self.export.exported if self.export is not None else 0


def run_fetch_only_job(
    *,
    session_factory: sessionmaker[Session],
    sources: list[SourceSpec] | tuple[SourceSpec, ...],
    router: Any,
    output_dir: str | Path = "output/fetch",
    limit_per_source: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    run_id: int | None = None,
) -> FetchOnlyResult:
    """Run only the collector/fetch stage, then export normalized rows.

    ``run_intel_fetch_job`` is the existing source-isolated persistence
    boundary.  The export query deliberately reads every item, including
    rows with status ``new``; later AI review is an explicit separate command.
    """

    fetch = run_intel_fetch_job(
        session_factory=session_factory,
        sources=sources,
        router=router,
        limit_per_source=limit_per_source,
        source_filter=source_filter,
        content_class=content_class,
        force=force,
        dry_run=dry_run,
        run_id=run_id,
    )
    if dry_run:
        return FetchOnlyResult(fetch=fetch)
    export = run_fetch_only_export_job(
        session_factory=session_factory,
        output_dir=output_dir,
        source_filter=source_filter,
        content_class=content_class,
    )
    return FetchOnlyResult(fetch=fetch, export=export)


def run_fetch_only_from_settings(
    *,
    settings: Settings,
    registry_path=DEFAULT_REGISTRY_PATH,
    output_dir: str | Path = "output/fetch",
    limit_per_source: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    run_id: int | None = None,
) -> FetchOnlyResult:
    """Settings-backed fetch-only entry point used by the CLI and scripts."""

    fetch = run_intel_fetch_from_settings(
        settings=settings,
        registry_path=registry_path,
        limit_per_source=limit_per_source,
        source_filter=source_filter,
        content_class=content_class,
        force=force,
        dry_run=dry_run,
        run_id=run_id,
    )
    if dry_run:
        return FetchOnlyResult(fetch=fetch)

    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    export = run_fetch_only_export_job(
        session_factory=create_session_factory(engine),
        output_dir=output_dir,
        source_filter=source_filter,
        content_class=content_class,
    )
    return FetchOnlyResult(fetch=fetch, export=export)


def run_fetch_only_export_job(
    *,
    session_factory: sessionmaker[Session],
    output_dir: str | Path = "output/fetch",
    limit: int | None = None,
    source_filter: str | None = None,
    content_class: str | None = None,
    dry_run: bool = False,
) -> FetchOnlyExportResult:
    """Export all fetched/normalized rows without invoking later stages."""

    with session_factory() as session:
        stmt = (
            select(IntelItem)
            .options(joinedload(IntelItem.source))
            .order_by(IntelItem.captured_at.desc(), IntelItem.id.asc())
        )
        if source_filter:
            stmt = stmt.where(IntelItem.source_id == source_filter)
        if content_class:
            stmt = stmt.where(IntelItem.content_class == content_class)
        if limit is not None:
            stmt = stmt.limit(limit)
        items = list(session.scalars(stmt).unique().all())
        records = [_serialize_fetch_item(item) for item in items]

    output = Path(output_dir)
    json_path = output / "fetch_items.json"
    jsonl_path = output / "fetch_items.jsonl"
    markdown_path = output / "fetch_items.md"
    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        jsonl_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        markdown_path.write_text(_render_markdown(records), encoding="utf-8")
    return FetchOnlyExportResult(
        exported=len(records),
        json_path=str(json_path),
        jsonl_path=str(jsonl_path),
        markdown_path=str(markdown_path),
        dry_run=dry_run,
    )


def _serialize_fetch_item(item: IntelItem) -> dict[str, Any]:
    source = item.source
    metadata = _source_metadata(source)
    metrics = _json(item.metrics_json, {})
    raw_payload = _json(item.raw_payload_json, {})
    normalized = {
        "item_id": item.id,
        "external_id": item.external_id,
        "title": item.title,
        "url": item.canonical_url,
        "summary": item.summary,
        "content_text": item.content_text,
        "published_at": _date(item.published_at),
        "captured_at": _date(item.captured_at),
        "discovered_at": _date(item.discovered_at),
        "content_class": item.content_class,
        "metrics": metrics,
        "status": item.status,
    }
    return {
        "record_type": "fetch_item",
        "id": item.id,
        "source_id": metadata["source_id"],
        "name": metadata["source_name"],
        "source_name": metadata["source_name"],
        "source_transport": metadata["source_transport"],
        "transport": metadata["transport"],
        "source_group": metadata["source_group"],
        "source_subtype": metadata["source_subtype"],
        "tier": metadata["tier"],
        "source_tier": metadata["source_tier"],
        "source_role": metadata["source_role"],
        "role": metadata["role"],
        "x_official": metadata["x_official"],
        "source": metadata,
        "item": normalized,
        "raw_payload": raw_payload,
        **normalized,
    }


def _source_metadata(source: Source | None) -> dict[str, Any]:
    source_id = source.id if source is not None else None
    transport = source.transport if source is not None else None
    group = source.source_group if source is not None else None
    tier = source.tier if source is not None else None
    role = source.source_role if source is not None else None
    return {
        "id": source_id,
        "name": source.name if source is not None else None,
        "source_id": source_id,
        "source_name": source.name if source is not None else None,
        "source_transport": transport,
        "transport": transport,
        "source_group": group,
        "source_subtype": source.source_subtype if source is not None else None,
        "tier": tier,
        "source_tier": tier,
        "source_role": role,
        "role": role,
        "x_official": group == "x_official",
        "source_url": source.url if source is not None else None,
        "content_class": source.content_class if source is not None else None,
        "primary_eligible": bool(source.primary_eligible) if source is not None else False,
        "account_url": source.account_url if source is not None else None,
    }


def _render_markdown(records: list[dict[str, Any]]) -> str:
    lines = ["# Fetch-only items", "", f"条目数：{len(records)}", ""]
    for index, record in enumerate(records, start=1):
        source = record.get("source") or {}
        lines.extend(
            [
                f"## {index}. {record.get('title') or '(untitled)' }",
                (
                    f"- 来源：`{source.get('source_name') or source.get('source_id')}` "
                    f"(`{source.get('source_id')}`) transport=`{source.get('source_transport')}` "
                    f"group=`{source.get('source_group')}` subtype=`{source.get('source_subtype')}` "
                    f"tier=`{source.get('tier')}` role=`{source.get('source_role')}` "
                    f"x_official=`{str(bool(source.get('x_official'))).lower()}`"
                ),
                f"- 类别：`{record.get('content_class')}` | 状态：`{record.get('status')}`",
                f"- 链接：{record.get('url') or '无'}",
                f"- 摘要：{record.get('summary') or '暂无摘要'}",
                "",
            ]
        )
    return "\n".join(lines)


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed


def _date(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "FetchOnlyExportResult",
    "FetchOnlyResult",
    "run_fetch_only_export_job",
    "run_fetch_only_from_settings",
    "run_fetch_only_job",
]
