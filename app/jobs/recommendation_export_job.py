from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import VerificationItemRepository


@dataclass(frozen=True)
class RecommendationExportResult:
    exported: int
    markdown_path: Path
    jsonl_path: Path


def run_recommendation_export_job(
    *,
    session_factory: sessionmaker[Session],
    output_dir: str | Path = "output",
    limit: int | None = 20,
) -> RecommendationExportResult:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = output_path / f"recommendations_{timestamp}.md"
    jsonl_path = output_path / f"recommendations_{timestamp}.jsonl"

    with session_factory() as session:
        rows = VerificationItemRepository(session).list_for_recommendation_export(limit=limit)

    records = [_verification_to_record(row) for row in rows]
    _write_jsonl(jsonl_path, records)
    _write_markdown(markdown_path, records)
    return RecommendationExportResult(exported=len(records), markdown_path=markdown_path, jsonl_path=jsonl_path)


def run_recommendation_export_from_settings(
    *,
    settings: Settings,
    output_dir: str | Path = "output",
    limit: int | None = 20,
) -> RecommendationExportResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_recommendation_export_job(session_factory=session_factory, output_dir=output_dir, limit=limit)


def _verification_to_record(verification) -> dict:
    candidate = verification.candidate_item
    item = candidate.normalized_item
    raw_item = item.raw_item
    claim = candidate.extracted_claim
    evidence = candidate.evidence_items
    return {
        "candidate_id": candidate.id,
        "title": item.title,
        "url": item.url,
        "source_id": raw_item.source_id,
        "source_group": candidate.source_group,
        "source_subtype": candidate.source_subtype,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "entity_name": claim.entity_name if claim else None,
        "entity_type": claim.entity_type if claim else None,
        "final_keep": verification.final_keep,
        "final_score": verification.final_score,
        "recommendation_level": verification.recommendation_level,
        "credibility_score": verification.credibility_score,
        "novelty_score": verification.novelty_score,
        "spam_risk_score": verification.spam_risk_score,
        "category": verification.category,
        "summary_cn": verification.summary_cn,
        "recommendation_reason": verification.recommendation_reason,
        "risk_reason": verification.risk_reason,
        "evidence_summary": _loads_json_list(verification.evidence_summary),
        "risk_flags": _loads_json_list(verification.risk_flags),
        "links": {
            "official": claim.official_url if claim else None,
            "github": claim.github_url if claim else None,
            "huggingface": claim.huggingface_url if claim else None,
            "producthunt": claim.producthunt_url if claim else None,
            "source": item.url,
        },
        "evidence_count": len(evidence),
        "evidence_domains": sorted({row.source_domain for row in evidence if row.source_domain}),
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _write_markdown(path: Path, records: list[dict]) -> None:
    lines = [
        f"# AI 工具情报日报 - {datetime.now().date().isoformat()}",
        "",
        f"- 导出时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 推荐项数量：{len(records)}",
        "",
    ]
    sections = [
        ("今日强推荐", lambda row: row["final_keep"] and row["recommendation_level"] in {"S", "A"}),
        ("值得关注", lambda row: row["final_keep"] and row["recommendation_level"] == "B"),
        ("仅归档", lambda row: not row["final_keep"] and row["recommendation_level"] in {"B", "C"}),
        ("被剔除的高风险内容", lambda row: not row["final_keep"] and row["recommendation_level"] == "D"),
    ]
    used_ids: set[int] = set()
    for title, predicate in sections:
        section_rows = [row for row in records if predicate(row) and row["candidate_id"] not in used_ids]
        lines.extend([f"## {title}", ""])
        if not section_rows:
            lines.extend(["（无）", ""])
            continue
        for index, record in enumerate(section_rows, start=1):
            used_ids.add(record["candidate_id"])
            lines.extend(
                [
                    f"### {index}. {record['entity_name'] or record['title']}",
                    "",
                    f"- 标题：{record['title']}",
                    f"- 分类：`{record['category'] or record['entity_type'] or 'unknown'}`",
                    f"- 推荐分：`{record['final_score']}` / `{record['recommendation_level']}`",
                    f"- 可信度：`{record['credibility_score']}`；垃圾风险：`{record['spam_risk_score']}`",
                    f"- 证据数：`{record['evidence_count']}`；证据域名：`{', '.join(record['evidence_domains'])}`",
                    f"- 摘要：{record['summary_cn'] or ''}",
                    f"- 推荐理由：{record['recommendation_reason'] or ''}",
                    f"- 风险提示：{record['risk_reason'] or ''}",
                    f"- 风险标签：`{', '.join(record['risk_flags'])}`",
                    f"- 链接：{_format_links(record['links'])}",
                    "",
                ]
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def _format_links(links: dict[str, str | None]) -> str:
    parts = [f"{name}: {url}" for name, url in links.items() if url]
    return " / ".join(parts) if parts else "（无）"


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
