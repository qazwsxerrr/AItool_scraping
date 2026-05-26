from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.repository import RecommendationCardRepository

LOGGER = logging.getLogger(__name__)


@dataclass
class RecommendationWriteJobResult:
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def run_recommendation_write_job(
    *,
    session_factory: sessionmaker[Session],
    limit: int | None = 100,
    force: bool = False,
    writer_version: str = "recommendation_writer_v1",
) -> RecommendationWriteJobResult:
    result = RecommendationWriteJobResult()
    with session_factory() as session:
        repo = RecommendationCardRepository(session)
        rows = repo.list_pending_for_write(limit=limit, force=force)
        for verification in rows:
            result.processed += 1
            try:
                card = _build_recommendation_card(verification)
                inserted = repo.upsert(
                    verification_item_id=verification.id,
                    entity_id=card["entity_id"],
                    title=card["title"],
                    summary_cn=card["summary_cn"],
                    why_recommend=card["why_recommend"],
                    how_to_try=card["how_to_try"],
                    risk_note=card["risk_note"],
                    evidence_note=card["evidence_note"],
                    raw_response=card,
                    writer_version=writer_version,
                    source_verification_updated_at=verification.updated_at or verification.created_at,
                )
                if inserted.inserted:
                    result.inserted += 1
                elif inserted.reason == "updated":
                    result.inserted += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                result.failed += 1
                result.errors.append(f"verification_id={verification.id}: {exc}")
                LOGGER.exception("Failed to write recommendation card for verification item %s", verification.id)
        session.commit()
    return result


def run_recommendation_write_from_settings(
    *,
    settings: Settings,
    limit: int | None = 100,
    force: bool = False,
    writer_version: str = "recommendation_writer_v1",
) -> RecommendationWriteJobResult:
    engine = create_engine_from_url(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    return run_recommendation_write_job(
        session_factory=session_factory,
        limit=limit,
        force=force,
        writer_version=writer_version,
    )


def _build_recommendation_card(verification) -> dict:
    candidate = verification.candidate_item
    item = candidate.normalized_item
    claim = candidate.extracted_claim
    entity = candidate.entity_mentions[0].entity if candidate.entity_mentions else None
    title = entity.name if entity else (claim.entity_name if claim and claim.entity_name else item.title)
    evidence_domains = sorted({row.source_domain for row in candidate.evidence_items if row.source_domain})
    supported_claims = [row for row in candidate.claim_verification_items if row.supports_claim == "support"]
    links = _links(claim, item.url)
    return {
        "entity_id": entity.id if entity else None,
        "title": title,
        "summary_cn": verification.summary_cn or f"{title} 是一条通过证据核实的 AI 工具情报。",
        "why_recommend": _join_sentences(
            [
                verification.recommendation_reason,
                f"推荐分 {verification.final_score}，可信度 {verification.credibility_score}。",
                f"已支持 {len(supported_claims)} 条关键 claim。" if supported_claims else None,
            ]
        ),
        "how_to_try": _how_to_try(links),
        "risk_note": verification.risk_reason or _risk_from_flags(verification.risk_flags),
        "evidence_note": _evidence_note(evidence_domains, supported_claims, verification.evidence_summary),
        "links": links,
        "method": "rule_writer_v1",
    }


def _links(claim, source_url: str | None) -> dict[str, str | None]:
    return {
        "official": claim.official_url if claim else None,
        "github": claim.github_url if claim else None,
        "huggingface": claim.huggingface_url if claim else None,
        "producthunt": claim.producthunt_url if claim else None,
        "source": source_url,
    }


def _how_to_try(links: dict[str, str | None]) -> str:
    if links.get("github"):
        return f"优先查看 GitHub README / quickstart：{links['github']}"
    if links.get("huggingface"):
        return f"打开 Hugging Face 页面确认模型卡、权重和 license：{links['huggingface']}"
    if links.get("official"):
        return f"打开官网查看文档、定价和使用入口：{links['official']}"
    if links.get("source"):
        return f"先查看原始来源并确认可用性：{links['source']}"
    return "建议先人工复核来源与可用入口。"


def _evidence_note(domains: list[str], supported_claims: list, evidence_summary: str | None) -> str:
    summary = _loads_json_list(evidence_summary)
    parts = []
    if domains:
        parts.append("证据域名：" + "、".join(domains[:5]))
    if supported_claims:
        parts.append(f"claim 级支持：{len(supported_claims)} 条")
    if summary:
        parts.append("；".join(summary[:2]))
    return "；".join(parts) if parts else "证据仍偏少，建议进入人工复核。"


def _risk_from_flags(value: str | None) -> str | None:
    flags = _loads_json_list(value)
    if not flags:
        return None
    return "风险标签：" + "、".join(flags)


def _join_sentences(parts: list[str | None]) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


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
