from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.ai.claim_client import ClaimExtractResponse
from app.config.source_registry import SourceConfig
from app.parsers.feed_parser import ParsedFeedItem
from app.ai.review_client import AIReviewResponse
from app.pipeline.verification import FinalVerification
from app.pipeline.normalize import NormalizedItemData
from app.pipeline.prefilter import CandidateDecision
from app.storage.models import (
    AIReviewItem,
    CandidateItem,
    EvidenceItem,
    ExtractedClaim,
    NormalizedItem,
    RawItem,
    Source,
    VerificationItem,
)


@dataclass(frozen=True)
class InsertResult:
    inserted: bool
    reason: str | None = None
    item_id: int | None = None


class SourceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_source(self, source: SourceConfig) -> Source:
        existing = self.session.get(Source, source.id)
        if existing is None:
            existing = Source(id=source.id)
            self.session.add(existing)

        existing.name = source.name
        existing.type = source.type
        existing.url = source.url
        existing.enabled = source.enabled
        existing.priority = source.priority
        existing.fetch_interval = source.fetch_interval
        existing.parser_type = source.parser_type
        return existing

    def mark_fetched(self, source_id: str, fetched_at: datetime | None = None) -> None:
        source = self.session.get(Source, source_id)
        if source is None:
            return
        source.last_fetched_at = fetched_at or datetime.now(timezone.utc)

    def get_source_metadata(self, source_id: str) -> tuple[str, str]:
        source = self.session.get(Source, source_id)
        if source is None:
            return "general", "fixed"
        return infer_source_group(source.id), infer_source_subtype(source.id)


class RawItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_new(self, item: ParsedFeedItem) -> InsertResult:
        duplicate_reason = self._find_duplicate_reason(item)
        if duplicate_reason:
            return InsertResult(inserted=False, reason=duplicate_reason)

        raw_item = RawItem(
            source_id=item.source_id,
            external_id=item.external_id,
            title=item.title,
            link=item.link,
            author=item.author,
            published_at=_as_utc(item.published_at),
            raw_summary=item.raw_summary,
            raw_content=item.raw_content,
            raw_payload=json.dumps(item.raw_payload, ensure_ascii=False, default=str),
            content_hash=item.content_hash,
            status="new",
        )
        self.session.add(raw_item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=raw_item.id)

    def _find_duplicate_reason(self, item: ParsedFeedItem) -> str | None:
        if item.external_id:
            stmt = select(RawItem.id).where(
                RawItem.source_id == item.source_id,
                RawItem.external_id == item.external_id,
            )
            if self.session.execute(stmt).first():
                return "duplicate_external_id"

        if item.link:
            stmt = select(RawItem.id).where(
                RawItem.source_id == item.source_id,
                RawItem.link == item.link,
            )
            if self.session.execute(stmt).first():
                return "duplicate_link"

        stmt = select(RawItem.id).where(RawItem.content_hash == item.content_hash)
        if self.session.execute(stmt).first():
            return "duplicate_content_hash"
        return None

    def list_pending_for_normalization(self, *, limit: int | None = None) -> list[RawItem]:
        stmt = select(RawItem).where(RawItem.status == "new").order_by(RawItem.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def mark_status(self, raw_item_id: int, status: str) -> None:
        raw_item = self.session.get(RawItem, raw_item_id)
        if raw_item is not None:
            raw_item.status = status


class NormalizedItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_new(self, item: NormalizedItemData) -> InsertResult:
        duplicate_reason = self._find_duplicate_reason(item)
        if duplicate_reason:
            return InsertResult(inserted=False, reason=duplicate_reason)

        normalized_item = NormalizedItem(
            raw_item_id=item.raw_item_id,
            title=item.title,
            body_text=item.body_text,
            url=item.url,
            author=item.author,
            published_at=_as_utc(item.published_at),
            language=item.language,
            dedupe_key=item.dedupe_key,
        )
        self.session.add(normalized_item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=normalized_item.id)

    def _find_duplicate_reason(self, item: NormalizedItemData) -> str | None:
        stmt = select(NormalizedItem.id).where(NormalizedItem.raw_item_id == item.raw_item_id)
        if self.session.execute(stmt).first():
            return "duplicate_raw_item"

        stmt = select(NormalizedItem.id).where(NormalizedItem.dedupe_key == item.dedupe_key)
        if self.session.execute(stmt).first():
            return "duplicate_dedupe_key"
        return None

    def list_pending_for_prefilter(self, *, limit: int | None = None) -> list[NormalizedItem]:
        stmt = (
            select(NormalizedItem)
            .outerjoin(CandidateItem, CandidateItem.normalized_item_id == NormalizedItem.id)
            .where(CandidateItem.id.is_(None))
            .order_by(NormalizedItem.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())


class CandidateItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_new(
        self,
        *,
        normalized_item_id: int,
        source_group: str,
        source_subtype: str,
        decision: CandidateDecision,
    ) -> InsertResult:
        stmt = select(CandidateItem.id).where(CandidateItem.normalized_item_id == normalized_item_id)
        if self.session.execute(stmt).first():
            return InsertResult(inserted=False, reason="duplicate_normalized_item")

        candidate = CandidateItem(
            normalized_item_id=normalized_item_id,
            source_group=source_group,
            source_subtype=source_subtype,
            candidate_score=decision.score,
            matched_keywords=json.dumps(decision.matched_keywords, ensure_ascii=False),
            keep_reason=";".join(decision.keep_reasons) or None,
            drop_reason=";".join(decision.drop_reasons) or None,
            status="kept" if decision.keep else "dropped",
        )
        self.session.add(candidate)
        self.session.flush()
        return InsertResult(inserted=True, item_id=candidate.id)

    def list_for_review_export(self, *, status: str = "kept", limit: int | None = 50) -> list[CandidateItem]:
        stmt = (
            select(CandidateItem)
            .options(
                joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source)
            )
            .where(CandidateItem.status == status)
            .order_by(CandidateItem.candidate_score.desc(), CandidateItem.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def list_pending_for_ai_review(
        self,
        *,
        limit: int | None = 50,
        min_score: int = 70,
    ) -> list[CandidateItem]:
        stmt = (
            select(CandidateItem)
            .join(NormalizedItem, NormalizedItem.id == CandidateItem.normalized_item_id)
            .options(
                joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source)
            )
            .outerjoin(AIReviewItem, AIReviewItem.candidate_item_id == CandidateItem.id)
            .where(
                CandidateItem.status == "kept",
                CandidateItem.candidate_score >= min_score,
                AIReviewItem.id.is_(None),
            )
            .order_by(
                CandidateItem.candidate_score.desc(),
                NormalizedItem.published_at.desc(),
                CandidateItem.id.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def list_pending_for_claim_extract(
        self,
        *,
        limit: int | None = 50,
        min_ai_score: int = 70,
    ) -> list[CandidateItem]:
        stmt = (
            select(CandidateItem)
            .join(AIReviewItem, AIReviewItem.candidate_item_id == CandidateItem.id)
            .join(NormalizedItem, NormalizedItem.id == CandidateItem.normalized_item_id)
            .options(
                joinedload(CandidateItem.ai_review_item),
                joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source),
            )
            .outerjoin(ExtractedClaim, ExtractedClaim.candidate_item_id == CandidateItem.id)
            .where(
                CandidateItem.status == "kept",
                AIReviewItem.ai_keep.is_(True),
                AIReviewItem.ai_score >= min_ai_score,
                ExtractedClaim.id.is_(None),
            )
            .order_by(
                AIReviewItem.ai_score.desc(),
                CandidateItem.candidate_score.desc(),
                NormalizedItem.published_at.desc(),
                CandidateItem.id.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())


class AIReviewItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_new(
        self,
        *,
        candidate_item_id: int,
        model: str | None,
        response: AIReviewResponse,
    ) -> InsertResult:
        stmt = select(AIReviewItem.id).where(AIReviewItem.candidate_item_id == candidate_item_id)
        if self.session.execute(stmt).first():
            return InsertResult(inserted=False, reason="duplicate_candidate_item")

        item = AIReviewItem(
            candidate_item_id=candidate_item_id,
            model=model,
            ai_keep=response.keep,
            ai_score=response.score,
            category=response.category,
            reason=response.reason,
            summary_cn=response.summary_cn,
            raw_response=json.dumps(response.raw_response or {}, ensure_ascii=False, default=str),
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)


class ExtractedClaimRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_new(
        self,
        *,
        candidate_item_id: int,
        model: str | None,
        response: ClaimExtractResponse,
    ) -> InsertResult:
        stmt = select(ExtractedClaim.id).where(ExtractedClaim.candidate_item_id == candidate_item_id)
        if self.session.execute(stmt).first():
            return InsertResult(inserted=False, reason="duplicate_candidate_item")

        item = ExtractedClaim(
            candidate_item_id=candidate_item_id,
            model=model,
            entity_name=response.entity_name,
            entity_type=response.entity_type,
            official_url=response.official_url,
            github_url=response.github_url,
            huggingface_url=response.huggingface_url,
            producthunt_url=response.producthunt_url,
            claims_json=json.dumps(response.main_claims, ensure_ascii=False),
            release_signal=response.release_signal,
            actionable_signal=response.actionable_signal,
            confidence=response.confidence,
            raw_response=json.dumps(response.raw_response or {}, ensure_ascii=False, default=str),
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def list_pending_for_evidence_search(self, *, limit: int | None = 30) -> list[ExtractedClaim]:
        stmt = (
            select(ExtractedClaim)
            .join(CandidateItem, CandidateItem.id == ExtractedClaim.candidate_item_id)
            .options(
                joinedload(ExtractedClaim.candidate_item)
                .joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source)
            )
            .outerjoin(EvidenceItem, EvidenceItem.candidate_item_id == ExtractedClaim.candidate_item_id)
            .where(EvidenceItem.id.is_(None))
            .order_by(ExtractedClaim.confidence.desc(), ExtractedClaim.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())


class EvidenceItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_new(
        self,
        *,
        candidate_item_id: int,
        url: str,
        evidence_type: str,
        title: str | None,
        snippet: str | None,
        source_domain: str | None,
        supports_claim: str = "unknown",
        confidence: int = 0,
        raw_payload: dict | str | None = None,
        query: str | None = None,
    ) -> InsertResult:
        stmt = select(EvidenceItem.id).where(
            EvidenceItem.candidate_item_id == candidate_item_id,
            EvidenceItem.url == url,
        )
        if self.session.execute(stmt).first():
            return InsertResult(inserted=False, reason="duplicate_candidate_url")

        if isinstance(raw_payload, str):
            raw_text = raw_payload
        else:
            raw_text = json.dumps(raw_payload or {}, ensure_ascii=False, default=str)
        item = EvidenceItem(
            candidate_item_id=candidate_item_id,
            query=query,
            evidence_type=evidence_type,
            url=url,
            title=title,
            snippet=snippet,
            source_domain=source_domain,
            supports_claim=supports_claim,
            confidence=max(0, min(int(confidence), 100)),
            raw_payload=raw_text,
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def list_by_candidate(self, candidate_item_id: int) -> list[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .where(EvidenceItem.candidate_item_id == candidate_item_id)
            .order_by(EvidenceItem.confidence.desc(), EvidenceItem.id.asc())
        )
        return list(self.session.scalars(stmt).all())


class VerificationItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_pending_for_ai_verify(self, *, limit: int | None = 30) -> list[CandidateItem]:
        stmt = (
            select(CandidateItem)
            .join(AIReviewItem, AIReviewItem.candidate_item_id == CandidateItem.id)
            .join(ExtractedClaim, ExtractedClaim.candidate_item_id == CandidateItem.id)
            .options(
                joinedload(CandidateItem.ai_review_item),
                joinedload(CandidateItem.extracted_claim),
                selectinload(CandidateItem.evidence_items),
                joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source),
            )
            .outerjoin(VerificationItem, VerificationItem.candidate_item_id == CandidateItem.id)
            .where(
                CandidateItem.status == "kept",
                AIReviewItem.ai_keep.is_(True),
                VerificationItem.id.is_(None),
            )
            .order_by(
                AIReviewItem.ai_score.desc(),
                CandidateItem.candidate_score.desc(),
                CandidateItem.id.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def insert_if_new(
        self,
        *,
        candidate_item_id: int,
        model: str | None,
        verification: FinalVerification,
    ) -> InsertResult:
        stmt = select(VerificationItem.id).where(VerificationItem.candidate_item_id == candidate_item_id)
        if self.session.execute(stmt).first():
            return InsertResult(inserted=False, reason="duplicate_candidate_item")

        item = VerificationItem(
            candidate_item_id=candidate_item_id,
            model=model,
            verified=verification.verified,
            final_keep=verification.final_keep,
            final_score=verification.final_score,
            recommendation_level=verification.recommendation_level,
            relevance_score=verification.relevance_score,
            usefulness_score=verification.usefulness_score,
            credibility_score=verification.credibility_score,
            novelty_score=verification.novelty_score,
            reproducibility_score=verification.reproducibility_score,
            audience_fit_score=verification.audience_fit_score,
            source_quality_score=verification.source_quality_score,
            spam_risk_score=verification.spam_risk_score,
            category=verification.category,
            summary_cn=verification.summary_cn,
            recommendation_reason=verification.recommendation_reason,
            risk_reason=verification.risk_reason,
            evidence_summary=json.dumps(verification.evidence_summary, ensure_ascii=False),
            risk_flags=json.dumps(verification.risk_flags, ensure_ascii=False),
            raw_response=json.dumps(verification.raw_response or {}, ensure_ascii=False, default=str),
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def list_for_recommendation_export(self, *, limit: int | None = 20) -> list[VerificationItem]:
        stmt = (
            select(VerificationItem)
            .join(CandidateItem, CandidateItem.id == VerificationItem.candidate_item_id)
            .options(
                joinedload(VerificationItem.candidate_item)
                .joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source),
                joinedload(VerificationItem.candidate_item).joinedload(CandidateItem.extracted_claim),
                joinedload(VerificationItem.candidate_item).selectinload(CandidateItem.evidence_items),
            )
            .order_by(
                VerificationItem.final_keep.desc(),
                VerificationItem.final_score.desc(),
                VerificationItem.credibility_score.desc(),
                VerificationItem.novelty_score.desc(),
                VerificationItem.source_quality_score.desc(),
                CandidateItem.id.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def infer_source_group(source_id: str) -> str:
    if source_id.startswith("linux_do"):
        return "linux_do"
    if source_id.startswith("reddit_local_llama"):
        return "reddit_local_llama"
    if source_id.startswith("x_"):
        return "x"
    if source_id.startswith("producthunt"):
        return "producthunt"
    if source_id in {"openai_news", "google_deepmind_blog", "huggingface_blog"}:
        return "official_blog"
    return "general"


def infer_source_subtype(source_id: str) -> str:
    if "_top_day" in source_id:
        return "fixed_top_day"
    if "_top_week" in source_id:
        return "fixed_top_week"
    if source_id.endswith("_top") or "_top_" in source_id:
        return "fixed_top"
    if source_id.endswith("_hot") or "_hot_" in source_id:
        return "fixed_hot"
    if source_id.endswith("_new") or "_new_" in source_id:
        return "fixed_new"
    if "_search_" in source_id:
        return "search"
    if source_id.startswith("x_account_"):
        return "account"
    return "fixed"
