from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import EntityRepository, UserFeedbackRepository, VerificationItemRepository


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
    final_keep_only: bool = True,
) -> RecommendationExportResult:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = output_path / f"recommendations_{timestamp}.md"
    jsonl_path = output_path / f"recommendations_{timestamp}.jsonl"

    query_limit = limit * 5 if limit is not None and final_keep_only else limit
    with session_factory() as session:
        rows = VerificationItemRepository(session).list_for_recommendation_export(
            limit=query_limit,
            final_keep_only=final_keep_only,
        )

    records = [_verification_to_record(row) for row in rows]
    records.sort(
        key=lambda row: (
            -int(row["rerank_score"]),
            -int(row["final_score"]),
            -int(row["credibility_score"]),
            row["candidate_id"],
        )
    )
    if final_keep_only:
        records = _dedupe_records_by_entity(records)
    if limit is not None:
        records = records[:limit]
    _write_jsonl(jsonl_path, records)
    _write_markdown(markdown_path, records)
    if final_keep_only:
        with session_factory() as session:
            EntityRepository(session).mark_entities_recommended(
                [int(row["entity_id"]) for row in records if row.get("entity_id")]
            )
            session.commit()
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


def run_audit_export_job(
    *,
    session_factory: sessionmaker[Session],
    output_dir: str | Path = "output",
    limit: int | None = 100,
) -> RecommendationExportResult:
    return run_recommendation_export_job(
        session_factory=session_factory,
        output_dir=output_dir,
        limit=limit,
        final_keep_only=False,
    )


def run_audit_export_from_settings(
    *,
    settings: Settings,
    output_dir: str | Path = "output",
    limit: int | None = 100,
) -> RecommendationExportResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_audit_export_job(session_factory=session_factory, output_dir=output_dir, limit=limit)


def _verification_to_record(verification) -> dict:
    candidate = verification.candidate_item
    item = candidate.normalized_item
    raw_item = item.raw_item
    claim = candidate.extracted_claim
    evidence = candidate.evidence_items
    entity = candidate.entity_mentions[0].entity if candidate.entity_mentions else None
    feedback_summary = _feedback_summary_from_loaded(
        list(entity.feedback_items if entity else []) + list(candidate.feedback_items)
    )
    card = verification.recommendation_card
    update_reason = entity.last_update_reason if entity else None
    feedback_adjustment = _feedback_adjustment(feedback_summary)
    freshness_bonus = _freshness_bonus(verification.freshness_score)
    update_bonus = 4 if entity and entity.major_update_detected else 0
    rerank_score = _clamp_score(verification.final_score + feedback_adjustment + freshness_bonus + update_bonus)
    raw_response = _loads_json_dict(verification.raw_response)
    return {
        "candidate_id": candidate.id,
        "title": item.title,
        "url": item.url,
        "source_id": raw_item.source_id,
        "source_group": candidate.source_group,
        "source_subtype": candidate.source_subtype,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "entity_id": entity.id if entity else None,
        "entity_name": entity.name if entity else (claim.entity_name if claim else None),
        "entity_type": entity.entity_type if entity else (claim.entity_type if claim else None),
        "mention_count": entity.mention_count if entity else 1,
        "source_count": entity.source_count if entity else 1,
        "feedback": feedback_summary,
        "final_keep": verification.final_keep,
        "final_score": verification.final_score,
        "freshness_score": verification.freshness_score,
        "verification_version": verification.verification_version,
        "source_claim_verification_updated_at": _datetime_to_iso(
            verification.source_claim_verification_updated_at
        ),
        "stale": bool(verification.stale),
        "updated_at": _datetime_to_iso(verification.updated_at),
        "raw_response": raw_response,
        "feedback_adjustment": feedback_adjustment,
        "freshness_bonus": freshness_bonus,
        "rerank_score": rerank_score,
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
        "evidence_status": claim.evidence_status if claim else None,
        "evidence_items": [
            {
                "id": row.id,
                "evidence_type": row.evidence_type,
                "url": row.url,
                "source_domain": row.source_domain,
                "supports_claim": row.supports_claim,
                "evidence_confidence": row.evidence_confidence,
                "url_validation_status": row.url_validation_status,
                "http_status": row.http_status,
                "fetch_status": row.fetch_status,
                "classify_status": row.classify_status,
                "classified_at": _datetime_to_iso(row.classified_at),
                "classify_error": row.classify_error,
                "classification_version": row.classification_version,
                "risk_flags": _loads_json_list(row.risk_flags),
                "quality_flags": _loads_json_list(row.quality_flags),
                "updated_at": _datetime_to_iso(row.updated_at),
            }
            for row in sorted(evidence, key=lambda item: (-item.evidence_confidence, -item.retrieval_score, item.id))
        ],
        "claim_verifications": [
            {
                "claim_text": row.claim_text,
                "supports_claim": row.supports_claim,
                "support_strength": row.support_strength,
                "confidence": row.confidence,
                "risk_flags": _loads_json_list(row.risk_flags),
                "verification_version": row.verification_version,
                "source_evidence_updated_at": _datetime_to_iso(row.source_evidence_updated_at),
                "stale": bool(row.stale),
                "updated_at": _datetime_to_iso(row.updated_at),
            }
            for row in sorted(candidate.claim_verification_items, key=lambda item: (item.claim_index, item.id))
        ],
        "major_update_detected": bool(entity.major_update_detected) if entity else False,
        "update_reason": update_reason,
        "recommendation_card": (
            {
                "title": card.title,
                "summary_cn": card.summary_cn,
                "why_recommend": card.why_recommend,
                "how_to_try": card.how_to_try,
                "risk_note": card.risk_note,
                "evidence_note": card.evidence_note,
                "writer_version": card.writer_version,
                "source_verification_updated_at": _datetime_to_iso(card.source_verification_updated_at),
                "stale": bool(card.stale),
                "updated_at": _datetime_to_iso(card.updated_at),
            }
            if card
            else None
        ),
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _dedupe_records_by_entity(records: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for record in records:
        key = f"entity:{record['entity_id']}" if record.get("entity_id") else f"candidate:{record['candidate_id']}"
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


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
                    f"- 原始来源：{record.get('url') or (record.get('links') or {}).get('source') or '无'}",
                    f"- 来源标识：`{record.get('source_id') or 'unknown'}`；来源组：`{record.get('source_group') or 'unknown'}`；子类型：`{record.get('source_subtype') or 'unknown'}`",
                    f"- 分类：`{record['category'] or record['entity_type'] or 'unknown'}`",
                    f"- 推荐分：`{record['final_score']}` / `{record['recommendation_level']}`",
                    f"- 推荐排序分：`{record['rerank_score']}`（反馈调整 `{record['feedback_adjustment']}`；新鲜度 `{record['freshness_score']}`）",
                    f"- 可信度：`{record['credibility_score']}`；垃圾风险：`{record['spam_risk_score']}`",
                    f"- 证据状态：`{record['evidence_status'] or 'unknown'}`",
                    f"- 证据数：`{record['evidence_count']}`；证据域名：`{', '.join(record['evidence_domains'])}`",
                    f"- 更新信号：`{record['update_reason'] or 'none'}`",
                    f"- 摘要：{record['summary_cn'] or ''}",
                    f"- 推荐理由：{record['recommendation_reason'] or ''}",
                    f"- 怎么试：{(record.get('recommendation_card') or {}).get('how_to_try') or ''}",
                    f"- 证据说明：{(record.get('recommendation_card') or {}).get('evidence_note') or ''}",
                    f"- 风险提示：{record['risk_reason'] or ''}",
                    f"- 风险标签：`{', '.join(record['risk_flags'])}`",
                    f"- 来源/相关链接：{_format_links(record['links'])}",
                    f"- 证据链接：{_format_evidence_links(record.get('evidence_items') or [])}",
                    "",
                ]
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def _format_links(links: dict[str, str | None]) -> str:
    parts = [f"{name}: {url}" for name, url in links.items() if url]
    return " / ".join(parts) if parts else "（无）"


def _format_evidence_links(evidence_items: list[dict]) -> str:
    parts: list[str] = []
    for item in evidence_items[:5]:
        url = item.get("url")
        if not url:
            continue
        domain = item.get("source_domain") or "unknown"
        relation = item.get("relation") or item.get("support_status") or item.get("evidence_type") or "evidence"
        parts.append(f"{domain} ({relation}): {url}")
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


def _loads_json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _datetime_to_iso(value) -> str | None:
    return value.isoformat() if value else None


def _feedback_summary_from_loaded(rows) -> dict:
    actions: dict[str, int] = {}
    for row in rows:
        actions[row.action] = actions.get(row.action, 0) + 1
    positive = sum(actions.get(action, 0) for action in UserFeedbackRepository.POSITIVE_ACTIONS)
    negative = sum(actions.get(action, 0) for action in UserFeedbackRepository.NEGATIVE_ACTIONS)
    return {"total": len(rows), "positive": positive, "negative": negative, "actions": actions}


def _feedback_adjustment(summary: dict) -> int:
    actions = summary.get("actions") or {}
    adjustment = (
        int(actions.get("like", 0)) * 4
        + int(actions.get("save", 0)) * 6
        + int(actions.get("click", 0)) * 2
        - int(actions.get("dislike", 0)) * 6
        - int(actions.get("hide", 0)) * 12
        - int(actions.get("report", 0)) * 20
    )
    return max(-30, min(30, adjustment))


def _freshness_bonus(score: int) -> int:
    if score >= 90:
        return 8
    if score >= 80:
        return 6
    if score >= 65:
        return 3
    if score >= 45:
        return 0
    if score >= 25:
        return -3
    return -6


def _clamp_score(value: int | float) -> int:
    return max(0, min(int(value), 100))
