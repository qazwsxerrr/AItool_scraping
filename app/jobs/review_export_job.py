from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import CandidateItemRepository


@dataclass(frozen=True)
class ReviewExportResult:
    exported: int
    markdown_path: Path
    jsonl_path: Path


def run_review_export_job(
    *,
    session_factory: sessionmaker[Session],
    output_dir: str | Path = "output",
    limit: int | None = 50,
    status: str = "kept",
) -> ReviewExportResult:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = output_path / f"review_candidates_{timestamp}.md"
    jsonl_path = output_path / f"review_candidates_{timestamp}.jsonl"

    with session_factory() as session:
        rows = CandidateItemRepository(session).list_for_review_export(status=status, limit=limit)

    records = [_candidate_to_record(row) for row in rows]
    _write_markdown(markdown_path, records)
    _write_jsonl(jsonl_path, records)
    return ReviewExportResult(exported=len(records), markdown_path=markdown_path, jsonl_path=jsonl_path)


def run_review_export_from_settings(
    *,
    settings: Settings,
    output_dir: str | Path = "output",
    limit: int | None = 50,
    status: str = "kept",
) -> ReviewExportResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_review_export_job(session_factory=session_factory, output_dir=output_dir, limit=limit, status=status)


def _candidate_to_record(candidate) -> dict:
    item = candidate.normalized_item
    raw_item = item.raw_item
    body_text = item.body_text or ""
    return {
        "candidate_id": candidate.id,
        "normalized_item_id": candidate.normalized_item_id,
        "raw_item_id": item.raw_item_id,
        "source_id": raw_item.source_id,
        "source_group": candidate.source_group,
        "source_subtype": candidate.source_subtype,
        "status": candidate.status,
        "candidate_score": candidate.candidate_score,
        "matched_keywords": _loads_json_list(candidate.matched_keywords),
        "keep_reason": candidate.keep_reason,
        "drop_reason": candidate.drop_reason,
        "title": item.title,
        "url": item.url,
        "author": item.author,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "language": item.language,
        "body_preview": _truncate(body_text, 500),
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _write_markdown(path: Path, records: list[dict]) -> None:
    lines = ["# AI 初筛前人工审阅候选", "", f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}", f"- 候选数量：{len(records)}", ""]
    for index, record in enumerate(records, start=1):
        lines.extend(
            [
                f"## {index}. {record['title']}",
                "",
                f"- candidate_id: `{record['candidate_id']}`",
                f"- source: `{record['source_group']}` / `{record['source_subtype']}` / `{record['source_id']}`",
                f"- score: `{record['candidate_score']}`",
                f"- keywords: `{', '.join(record['matched_keywords'])}`",
                f"- reason: `{record['keep_reason'] or record['drop_reason'] or ''}`",
                f"- url: {record['url'] or ''}",
                "",
                record["body_preview"] or "（无正文预览）",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _loads_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _truncate(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
