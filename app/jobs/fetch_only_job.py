"""Diagnostic fetch export that never writes the formal daily workspace."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.config.limits import DEFAULT_FETCH_LIMIT_PER_SOURCE
from app.config.settings import Settings
from app.config.source_registry import DEFAULT_REGISTRY_PATH
from app.domain.models import FetchItem, SourceSpec
from app.jobs.fetch_job import DiagnosticFetchedItem, IntelFetchResult, run_intel_fetch_from_settings, run_intel_fetch_job


@dataclass(frozen=True)
class FetchOnlyExportResult:
    exported: int
    json_path: str
    jsonl_path: str
    markdown_path: str


@dataclass(frozen=True)
class FetchOnlyResult:
    fetch: IntelFetchResult
    export: FetchOnlyExportResult

    @property
    def exported(self) -> int:
        return self.export.exported


def run_fetch_only_job(
    *,
    sources: Iterable[SourceSpec],
    router: Any,
    output_dir: str | Path = "output/fetch",
    limit_per_source: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
) -> FetchOnlyResult:
    """Collect and export diagnostic data without database writes."""

    fetch = run_intel_fetch_job(
        session_factory=None,  # dry-run collection never opens a session
        sources=sources,
        router=router,
        limit_per_source=limit_per_source,
        source_filter=source_filter,
        content_class=content_class,
        force=force,
        dry_run=True,
    )
    return FetchOnlyResult(fetch=fetch, export=_write_fetch_export(fetch.diagnostic_items, output_dir=output_dir))


def run_fetch_only_from_settings(
    *,
    settings: Settings,
    registry_path=DEFAULT_REGISTRY_PATH,
    output_dir: str | Path = "output/fetch",
    limit_per_source: int | None = DEFAULT_FETCH_LIMIT_PER_SOURCE,
    source_filter: str | None = None,
    content_class: str | None = None,
    force: bool = False,
) -> FetchOnlyResult:
    """Settings-backed diagnostic fetch/export entry point."""

    fetch = run_intel_fetch_from_settings(
        settings=settings,
        registry_path=registry_path,
        limit_per_source=limit_per_source,
        source_filter=source_filter,
        content_class=content_class,
        force=force,
        dry_run=True,
    )
    return FetchOnlyResult(fetch=fetch, export=_write_fetch_export(fetch.diagnostic_items, output_dir=output_dir))


def _write_fetch_export(items: Iterable[DiagnosticFetchedItem], *, output_dir: str | Path) -> FetchOnlyExportResult:
    records = [_serialize_diagnostic_item(value.source, value.item) for value in items]
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "fetch_items.json"
    jsonl_path = output / "fetch_items.jsonl"
    markdown_path = output / "fetch_items.md"
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    jsonl_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n" for record in records),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(records), encoding="utf-8")
    return FetchOnlyExportResult(
        exported=len(records),
        json_path=str(json_path),
        jsonl_path=str(jsonl_path),
        markdown_path=str(markdown_path),
    )


def _serialize_diagnostic_item(source: SourceSpec, item: FetchItem) -> dict[str, Any]:
    source_metadata = {
        "source_id": source.id,
        "source_name": source.name,
        "source_transport": source.transport,
        "source_group": source.source_group,
        "source_subtype": source.source_subtype,
        "source_role": source.source_role,
        "source_url": source.url,
        "content_class": source.content_class,
    }
    normalized = {
        "external_id": item.external_id,
        "title": item.title,
        "url": item.url,
        "summary": item.summary,
        "content_text": item.content,
        "published_at": _date(item.published_at),
        "captured_at": _date(item.captured_at),
        "content_class": item.content_class or source.content_class,
        "metrics": item.metrics,
    }
    return {
        "record_type": "fetch_item",
        "source": source_metadata,
        "raw_payload": item.raw_payload,
        **source_metadata,
        **normalized,
    }


def _render_markdown(records: list[dict[str, Any]]) -> str:
    lines = ["# Fetch-only items", "", f"条目数：{len(records)}", ""]
    for index, record in enumerate(records, start=1):
        lines.extend(
            [
                f"## {index}. {record.get('title') or '(untitled)'}",
                f"- 来源：`{record.get('source_name') or record.get('source_id')}` (`{record.get('source_id')}`)",
                f"- 链接：{record.get('url') or '无'}",
                f"- 摘要：{record.get('summary') or '暂无摘要'}",
                "",
            ]
        )
    return "\n".join(lines)


def _date(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "FetchOnlyExportResult",
    "FetchOnlyResult",
    "run_fetch_only_from_settings",
    "run_fetch_only_job",
]
