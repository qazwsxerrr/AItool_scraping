from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
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
    CanonicalEntity,
    ClaimVerificationItem,
    EntityMention,
    EvidenceItem,
    ExtractedClaim,
    NormalizedItem,
    PipelineRun,
    RawItem,
    RecommendationCard,
    SearchCacheItem,
    Source,
    UserFeedback,
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
        existing.source_group = source.source_group
        existing.source_subtype = source.source_subtype
        existing.quality_weight = source.quality_weight
        existing.source_role = source.source_role
        existing.spam_risk = source.spam_risk
        existing.requires_verification = source.requires_verification
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
        return source.source_group, source.source_subtype


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
            evidence_status="pending",
            evidence_attempts=0,
            evidence_error=None,
            evidence_searched_at=None,
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def list_pending_for_evidence_search(
        self,
        *,
        limit: int | None = 30,
        max_attempts: int = 3,
    ) -> list[ExtractedClaim]:
        stmt = (
            select(ExtractedClaim)
            .options(
                joinedload(ExtractedClaim.candidate_item)
                .joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source)
            )
            .where(
                ExtractedClaim.evidence_status.in_(["pending", "partial", "failed"]),
                ExtractedClaim.evidence_attempts < max_attempts,
            )
            .order_by(
                ExtractedClaim.evidence_attempts.asc(),
                ExtractedClaim.confidence.desc(),
                ExtractedClaim.id.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def mark_evidence_search_started(self, claim_id: int) -> None:
        claim = self.session.get(ExtractedClaim, claim_id)
        if claim is None:
            return
        claim.evidence_status = "searching"
        claim.evidence_attempts += 1
        claim.evidence_error = None

    def mark_evidence_search_finished(self, claim_id: int, *, status: str, error: str | None = None) -> None:
        claim = self.session.get(ExtractedClaim, claim_id)
        if claim is None:
            return
        claim.evidence_status = status
        claim.evidence_error = error
        claim.evidence_searched_at = datetime.now(timezone.utc)


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
        retrieval_score: int | None = None,
        evidence_confidence: int | None = None,
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
            retrieval_score=_clamp_score(confidence if retrieval_score is None else retrieval_score),
            evidence_confidence=_clamp_score(confidence if evidence_confidence is None else evidence_confidence),
            raw_payload=raw_text,
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def list_by_candidate(self, candidate_item_id: int) -> list[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .where(EvidenceItem.candidate_item_id == candidate_item_id)
            .order_by(EvidenceItem.evidence_confidence.desc(), EvidenceItem.retrieval_score.desc(), EvidenceItem.id.asc())
        )
        return list(self.session.scalars(stmt).all())

    def list_pending_for_fetch(self, *, limit: int | None = 50) -> list[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .options(joinedload(EvidenceItem.candidate_item).joinedload(CandidateItem.extracted_claim))
            .where(EvidenceItem.fetch_status.in_(["pending", "failed"]))
            .order_by(EvidenceItem.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def list_pending_for_classify(
        self,
        *,
        limit: int | None = 100,
        force: bool = False,
        classification_version: str | None = None,
    ) -> list[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .options(joinedload(EvidenceItem.candidate_item).joinedload(CandidateItem.extracted_claim))
            .where(EvidenceItem.fetch_status == "completed")
            .order_by(EvidenceItem.id.asc())
        )
        if not force:
            predicates = [EvidenceItem.classify_status.in_(["pending", "failed"])]
            if classification_version:
                predicates.append(EvidenceItem.classification_version != classification_version)
            stmt = stmt.where(or_(*predicates))
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def update_fetch_result(
        self,
        *,
        evidence_id: int,
        http_status: int | None,
        final_url: str | None,
        url_validation_status: str,
        fetched_title: str | None,
        fetched_description: str | None,
        fetched_text_preview: str | None,
        raw_payload: dict | str | None,
        fetch_status: str = "completed",
        fetch_error: str | None = None,
    ) -> None:
        item = self.session.get(EvidenceItem, evidence_id)
        if item is None:
            return
        item.http_status = http_status
        item.final_url = final_url
        item.url_validation_status = url_validation_status
        item.fetched_title = fetched_title
        item.fetched_description = fetched_description
        item.fetched_text_preview = fetched_text_preview
        item.fetch_status = fetch_status
        item.fetch_error = fetch_error
        item.fetched_at = datetime.now(timezone.utc)
        item.updated_at = item.fetched_at
        if raw_payload is not None:
            item.raw_payload = raw_payload if isinstance(raw_payload, str) else json.dumps(raw_payload, ensure_ascii=False, default=str)
        if fetch_status == "completed":
            item.classify_status = "pending"
            item.classified_at = None
            item.classify_error = None

    def update_classification(
        self,
        *,
        evidence_id: int,
        supports_claim: str,
        evidence_confidence: int,
        risk_flags: list[str],
        quality_flags: list[str],
        classification_version: str = "rules_v1",
    ) -> None:
        item = self.session.get(EvidenceItem, evidence_id)
        if item is None:
            return
        now = datetime.now(timezone.utc)
        item.supports_claim = supports_claim
        item.evidence_confidence = _clamp_score(evidence_confidence)
        item.confidence = _clamp_score(evidence_confidence)
        item.risk_flags = json.dumps(list(dict.fromkeys(risk_flags)), ensure_ascii=False)
        item.quality_flags = json.dumps(list(dict.fromkeys(quality_flags)), ensure_ascii=False)
        item.classify_status = "completed"
        item.classified_at = now
        item.classify_error = None
        item.classification_version = classification_version
        item.updated_at = now

    def mark_classification_failed(self, evidence_id: int, error: str) -> None:
        item = self.session.get(EvidenceItem, evidence_id)
        if item is None:
            return
        item.classify_status = "failed"
        item.classify_error = error
        item.updated_at = datetime.now(timezone.utc)


class ClaimVerificationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_pending_claims(self, *, limit: int | None = 100, force: bool = False) -> list[ExtractedClaim]:
        stmt = (
            select(ExtractedClaim)
            .join(CandidateItem, CandidateItem.id == ExtractedClaim.candidate_item_id)
            .options(
                selectinload(ExtractedClaim.claim_verification_items),
                joinedload(ExtractedClaim.candidate_item)
                .selectinload(CandidateItem.evidence_items),
                joinedload(ExtractedClaim.candidate_item)
                .joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source),
            )
            .order_by(ExtractedClaim.confidence.desc(), ExtractedClaim.id.asc())
        )
        rows = list(self.session.scalars(stmt).all())
        pending = [claim for claim in rows if force or _claim_needs_verification(claim)]
        if limit is not None:
            pending = pending[:limit]
        return pending

    def insert_if_new(
        self,
        *,
        candidate_item_id: int,
        extracted_claim_id: int,
        claim_index: int,
        claim_text: str,
        supports_claim: str,
        support_strength: str = "none",
        evidence_item_ids: list[int],
        confidence: int,
        risk_flags: list[str],
        raw_response: dict | str | None = None,
        verification_version: str = "claim_rules_v1",
        source_evidence_updated_at: datetime | None = None,
    ) -> InsertResult:
        stmt = select(ClaimVerificationItem.id).where(
            ClaimVerificationItem.extracted_claim_id == extracted_claim_id,
            ClaimVerificationItem.claim_index == claim_index,
        )
        if self.session.execute(stmt).first():
            return InsertResult(inserted=False, reason="duplicate_claim_index")
        raw_text = raw_response if isinstance(raw_response, str) else json.dumps(raw_response or {}, ensure_ascii=False, default=str)
        item = ClaimVerificationItem(
            candidate_item_id=candidate_item_id,
            extracted_claim_id=extracted_claim_id,
            claim_index=claim_index,
            claim_text=claim_text,
            supports_claim=supports_claim,
            support_strength=support_strength,
            evidence_item_ids_json=json.dumps(evidence_item_ids, ensure_ascii=False),
            confidence=_clamp_score(confidence),
            risk_flags=json.dumps(list(dict.fromkeys(risk_flags)), ensure_ascii=False),
            raw_response=raw_text,
            verification_version=verification_version,
            source_evidence_updated_at=source_evidence_updated_at,
            stale=False,
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def upsert(
        self,
        *,
        candidate_item_id: int,
        extracted_claim_id: int,
        claim_index: int,
        claim_text: str,
        supports_claim: str,
        support_strength: str,
        evidence_item_ids: list[int],
        confidence: int,
        risk_flags: list[str],
        raw_response: dict | str | None = None,
        verification_version: str = "claim_rules_v1",
        source_evidence_updated_at: datetime | None = None,
    ) -> InsertResult:
        stmt = select(ClaimVerificationItem).where(
            ClaimVerificationItem.extracted_claim_id == extracted_claim_id,
            ClaimVerificationItem.claim_index == claim_index,
        )
        item = self.session.scalars(stmt).first()
        created = False
        if item is None:
            item = ClaimVerificationItem(
                candidate_item_id=candidate_item_id,
                extracted_claim_id=extracted_claim_id,
                claim_index=claim_index,
            )
            self.session.add(item)
            created = True
        raw_text = raw_response if isinstance(raw_response, str) else json.dumps(raw_response or {}, ensure_ascii=False, default=str)
        now = datetime.now(timezone.utc)
        item.candidate_item_id = candidate_item_id
        item.extracted_claim_id = extracted_claim_id
        item.claim_index = claim_index
        item.claim_text = claim_text
        item.supports_claim = supports_claim
        item.support_strength = support_strength
        item.evidence_item_ids_json = json.dumps(evidence_item_ids, ensure_ascii=False)
        item.confidence = _clamp_score(confidence)
        item.risk_flags = json.dumps(list(dict.fromkeys(risk_flags)), ensure_ascii=False)
        item.raw_response = raw_text
        item.verification_version = verification_version
        item.source_evidence_updated_at = source_evidence_updated_at
        item.stale = False
        item.updated_at = now
        self.session.flush()
        return InsertResult(inserted=created, reason=None if created else "updated", item_id=item.id)


class SearchCacheRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_fresh(self, *, provider: str, query: str) -> SearchCacheItem | None:
        query_hash = _query_hash(query)
        now = datetime.now(timezone.utc)
        stmt = select(SearchCacheItem).where(
            SearchCacheItem.provider == provider,
            SearchCacheItem.query_hash == query_hash,
        )
        item = self.session.scalars(stmt).first()
        if item is None:
            return None
        expires_at = item.expires_at
        if expires_at is None:
            return item
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return None
        return item

    def upsert(
        self,
        *,
        provider: str,
        query: str,
        raw_response: dict,
        result_count: int,
        ttl_hours: int = 24,
    ) -> SearchCacheItem:
        query_hash = _query_hash(query)
        stmt = select(SearchCacheItem).where(
            SearchCacheItem.provider == provider,
            SearchCacheItem.query_hash == query_hash,
        )
        item = self.session.scalars(stmt).first()
        if item is None:
            item = SearchCacheItem(provider=provider, query=query, query_hash=query_hash)
            self.session.add(item)
        item.query = query
        item.raw_response = json.dumps(raw_response, ensure_ascii=False, default=str)
        item.result_count = result_count
        item.created_at = datetime.now(timezone.utc)
        item.expires_at = item.created_at + timedelta(hours=ttl_hours)
        self.session.flush()
        return item


class VerificationItemRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_pending_for_ai_verify(self, *, limit: int | None = 30, force: bool = False) -> list[CandidateItem]:
        stmt = (
            select(CandidateItem)
            .join(AIReviewItem, AIReviewItem.candidate_item_id == CandidateItem.id)
            .join(ExtractedClaim, ExtractedClaim.candidate_item_id == CandidateItem.id)
            .options(
                joinedload(CandidateItem.ai_review_item),
                joinedload(CandidateItem.extracted_claim).selectinload(ExtractedClaim.claim_verification_items),
                selectinload(CandidateItem.verification_item),
                selectinload(CandidateItem.evidence_items),
                joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source),
                selectinload(CandidateItem.claim_verification_items),
            )
            .where(
                CandidateItem.status == "kept",
                AIReviewItem.ai_keep.is_(True),
            )
            .order_by(
                AIReviewItem.ai_score.desc(),
                CandidateItem.candidate_score.desc(),
                CandidateItem.id.asc(),
            )
        )
        rows = list(self.session.scalars(stmt).all())
        pending = [candidate for candidate in rows if force or _candidate_needs_ai_verification(candidate)]
        if limit is not None:
            pending = pending[:limit]
        return pending

    def insert_if_new(
        self,
        *,
        candidate_item_id: int,
        model: str | None,
        verification: FinalVerification,
        freshness_score: int = 0,
        verification_version: str = "ai_verify_v1",
        source_claim_verification_updated_at: datetime | None = None,
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
            freshness_score=_clamp_score(freshness_score),
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
            verification_version=verification_version,
            source_claim_verification_updated_at=source_claim_verification_updated_at,
            stale=False,
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def upsert(
        self,
        *,
        candidate_item_id: int,
        model: str | None,
        verification: FinalVerification,
        freshness_score: int = 0,
        verification_version: str = "ai_verify_v1",
        source_claim_verification_updated_at: datetime | None = None,
    ) -> InsertResult:
        item = self.session.scalars(
            select(VerificationItem).where(VerificationItem.candidate_item_id == candidate_item_id)
        ).first()
        created = False
        if item is None:
            item = VerificationItem(candidate_item_id=candidate_item_id)
            self.session.add(item)
            created = True

        now = datetime.now(timezone.utc)
        item.model = model
        item.verified = verification.verified
        item.final_keep = verification.final_keep
        item.final_score = verification.final_score
        item.freshness_score = _clamp_score(freshness_score)
        item.recommendation_level = verification.recommendation_level
        item.relevance_score = verification.relevance_score
        item.usefulness_score = verification.usefulness_score
        item.credibility_score = verification.credibility_score
        item.novelty_score = verification.novelty_score
        item.reproducibility_score = verification.reproducibility_score
        item.audience_fit_score = verification.audience_fit_score
        item.source_quality_score = verification.source_quality_score
        item.spam_risk_score = verification.spam_risk_score
        item.category = verification.category
        item.summary_cn = verification.summary_cn
        item.recommendation_reason = verification.recommendation_reason
        item.risk_reason = verification.risk_reason
        item.evidence_summary = json.dumps(verification.evidence_summary, ensure_ascii=False)
        item.risk_flags = json.dumps(verification.risk_flags, ensure_ascii=False)
        item.raw_response = json.dumps(verification.raw_response or {}, ensure_ascii=False, default=str)
        item.verification_version = verification_version
        item.source_claim_verification_updated_at = source_claim_verification_updated_at
        item.stale = False
        item.updated_at = now
        self.session.flush()
        return InsertResult(inserted=created, reason=None if created else "updated", item_id=item.id)

    def list_for_recommendation_export(
        self,
        *,
        limit: int | None = 20,
        final_keep_only: bool = True,
    ) -> list[VerificationItem]:
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
                joinedload(VerificationItem.candidate_item).selectinload(CandidateItem.claim_verification_items),
                joinedload(VerificationItem.candidate_item).selectinload(CandidateItem.feedback_items),
                selectinload(VerificationItem.recommendation_card),
                joinedload(VerificationItem.candidate_item)
                .selectinload(CandidateItem.entity_mentions)
                .joinedload(EntityMention.entity),
                joinedload(VerificationItem.candidate_item)
                .selectinload(CandidateItem.entity_mentions)
                .joinedload(EntityMention.entity)
                .selectinload(CanonicalEntity.feedback_items),
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
        if final_keep_only:
            stmt = stmt.where(
                VerificationItem.final_keep.is_(True),
                VerificationItem.recommendation_level.in_(["S", "A", "B"]),
            )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())


class EntityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_unmentioned_verifications(self, *, limit: int | None = 100) -> list[VerificationItem]:
        stmt = (
            select(VerificationItem)
            .join(CandidateItem, CandidateItem.id == VerificationItem.candidate_item_id)
            .options(
                joinedload(VerificationItem.candidate_item)
                .joinedload(CandidateItem.normalized_item)
                .joinedload(NormalizedItem.raw_item)
                .joinedload(RawItem.source),
                joinedload(VerificationItem.candidate_item).joinedload(CandidateItem.extracted_claim),
            )
            .outerjoin(EntityMention, EntityMention.verification_item_id == VerificationItem.id)
            .where(VerificationItem.final_keep.is_(True), EntityMention.id.is_(None))
            .order_by(VerificationItem.final_score.desc(), VerificationItem.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def resolve_verification(self, verification: VerificationItem) -> tuple[CanonicalEntity, bool, bool]:
        candidate = verification.candidate_item
        claim = candidate.extracted_claim
        entity = self._find_entity(claim)
        created_entity = False
        previous_last_seen = entity.last_seen_at if entity is not None else None
        if entity is None:
            entity = CanonicalEntity(
                entity_type=(claim.entity_type if claim else verification.category) or "other",
                name=(claim.entity_name if claim and claim.entity_name else candidate.normalized_item.title),
                normalized_name=_normalize_entity_name(claim.entity_name if claim and claim.entity_name else candidate.normalized_item.title),
                canonical_url=claim.official_url if claim else candidate.normalized_item.url,
                github_url=claim.github_url if claim else None,
                huggingface_url=claim.huggingface_url if claim else None,
                producthunt_url=claim.producthunt_url if claim else None,
                first_seen_at=candidate.normalized_item.published_at,
                last_seen_at=candidate.normalized_item.published_at,
                best_score=verification.final_score,
                status="active",
            )
            self.session.add(entity)
            self.session.flush()
            created_entity = True
        else:
            self._fill_entity_links(entity, claim)

        mention_exists = self.session.execute(
            select(EntityMention.id).where(
                EntityMention.entity_id == entity.id,
                EntityMention.candidate_item_id == candidate.id,
            )
        ).first()
        created_mention = False
        if not mention_exists:
            raw_item = candidate.normalized_item.raw_item
            mention = EntityMention(
                entity_id=entity.id,
                candidate_item_id=candidate.id,
                verification_item_id=verification.id,
                source_id=raw_item.source_id,
                mention_url=candidate.normalized_item.url,
                mention_type="strong" if _strong_key(claim) else "name",
                confidence=95 if _strong_key(claim) else 70,
            )
            self.session.add(mention)
            self.session.flush()
            created_mention = True

        self._refresh_entity_stats(entity)
        is_major_update, update_reason = _detect_entity_update(
            verification=verification,
            previous_last_seen=previous_last_seen,
            created_entity=created_entity,
        )
        entity.major_update_detected = is_major_update
        if update_reason:
            entity.last_update_reason = update_reason
        return entity, created_entity, created_mention

    def mark_entities_recommended(self, entity_ids: list[int], *, recommended_at: datetime | None = None) -> int:
        if not entity_ids:
            return 0
        unique_ids = list(dict.fromkeys(entity_ids))
        recommended_time = recommended_at or datetime.now(timezone.utc)
        rows = list(self.session.scalars(select(CanonicalEntity).where(CanonicalEntity.id.in_(unique_ids))).all())
        for entity in rows:
            entity.last_recommended_at = recommended_time
        return len(rows)

    def _fill_entity_links(self, entity: CanonicalEntity, claim: ExtractedClaim | None) -> None:
        if claim is None:
            return
        if not entity.canonical_url and claim.official_url:
            entity.canonical_url = claim.official_url
        if not entity.github_url and claim.github_url:
            entity.github_url = claim.github_url
        if not entity.huggingface_url and claim.huggingface_url:
            entity.huggingface_url = claim.huggingface_url
        if not entity.producthunt_url and claim.producthunt_url:
            entity.producthunt_url = claim.producthunt_url

    def _find_entity(self, claim: ExtractedClaim | None) -> CanonicalEntity | None:
        if claim is None:
            return None
        if claim.github_url:
            entity = self.session.scalars(select(CanonicalEntity).where(CanonicalEntity.github_url == claim.github_url)).first()
            if entity:
                return entity
        if claim.huggingface_url:
            entity = self.session.scalars(select(CanonicalEntity).where(CanonicalEntity.huggingface_url == claim.huggingface_url)).first()
            if entity:
                return entity
        if claim.producthunt_url:
            entity = self.session.scalars(select(CanonicalEntity).where(CanonicalEntity.producthunt_url == claim.producthunt_url)).first()
            if entity:
                return entity
        if claim.official_url:
            entity = self.session.scalars(select(CanonicalEntity).where(CanonicalEntity.canonical_url == claim.official_url)).first()
            if entity:
                return entity
        if claim.entity_name:
            return self.session.scalars(
                select(CanonicalEntity).where(CanonicalEntity.normalized_name == _normalize_entity_name(claim.entity_name))
            ).first()
        return None

    def _refresh_entity_stats(self, entity: CanonicalEntity) -> None:
        mentions = list(
            self.session.scalars(
                select(EntityMention)
                .options(
                    joinedload(EntityMention.verification_item),
                    joinedload(EntityMention.candidate_item).joinedload(CandidateItem.normalized_item),
                )
                .where(EntityMention.entity_id == entity.id)
            ).all()
        )
        verification_scores = [
            mention.verification_item.final_score
            for mention in mentions
            if mention.verification_item is not None
        ]
        source_ids = {mention.source_id for mention in mentions}
        published_values = [
            mention.candidate_item.normalized_item.published_at
            for mention in mentions
            if mention.candidate_item and mention.candidate_item.normalized_item.published_at
        ]
        entity.best_score = max(verification_scores or [entity.best_score])
        entity.source_count = len(source_ids)
        entity.mention_count = len(mentions)
        if published_values:
            entity.first_seen_at = min(published_values)
            entity.last_seen_at = max(published_values)


class PipelineRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start(self, *, run_type: str) -> PipelineRun:
        item = PipelineRun(run_type=run_type, status="running", started_at=datetime.now(timezone.utc), stats_json="{}")
        self.session.add(item)
        self.session.flush()
        return item

    def finish(self, run_id: int, *, status: str, stats: dict, error: str | None = None) -> None:
        item = self.session.get(PipelineRun, run_id)
        if item is None:
            return
        item.status = status
        item.finished_at = datetime.now(timezone.utc)
        item.stats_json = json.dumps(stats, ensure_ascii=False, default=str)
        item.error = error


class UserFeedbackRepository:
    POSITIVE_ACTIONS = {"like", "save", "click"}
    NEGATIVE_ACTIONS = {"dislike", "hide", "report"}
    VALID_ACTIONS = POSITIVE_ACTIONS | NEGATIVE_ACTIONS

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(
        self,
        *,
        entity_id: int | None = None,
        candidate_item_id: int | None = None,
        action: str,
        reason: str | None = None,
    ) -> UserFeedback:
        normalized_action = action.strip().lower()
        if normalized_action not in self.VALID_ACTIONS:
            raise ValueError(f"unsupported feedback action: {action}")
        if entity_id is None and candidate_item_id is None:
            raise ValueError("entity_id or candidate_item_id is required")
        item = UserFeedback(
            entity_id=entity_id,
            candidate_item_id=candidate_item_id,
            action=normalized_action,
            reason=reason,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def summary(self, *, entity_id: int | None = None, candidate_item_id: int | None = None) -> dict:
        stmt = select(UserFeedback)
        if entity_id is not None:
            stmt = stmt.where(UserFeedback.entity_id == entity_id)
        if candidate_item_id is not None:
            stmt = stmt.where(UserFeedback.candidate_item_id == candidate_item_id)
        rows = list(self.session.scalars(stmt).all())
        actions: dict[str, int] = {}
        for row in rows:
            actions[row.action] = actions.get(row.action, 0) + 1
        positive = sum(actions.get(action, 0) for action in self.POSITIVE_ACTIONS)
        negative = sum(actions.get(action, 0) for action in self.NEGATIVE_ACTIONS)
        return {"total": len(rows), "positive": positive, "negative": negative, "actions": actions}


class RecommendationCardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_pending_for_write(self, *, limit: int | None = 100, force: bool = False) -> list[VerificationItem]:
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
                joinedload(VerificationItem.candidate_item).selectinload(CandidateItem.claim_verification_items),
                joinedload(VerificationItem.candidate_item)
                .selectinload(CandidateItem.entity_mentions)
                .joinedload(EntityMention.entity),
                selectinload(VerificationItem.recommendation_card),
            )
            .where(
                VerificationItem.final_keep.is_(True),
            )
            .order_by(
                VerificationItem.final_score.desc(),
                VerificationItem.freshness_score.desc(),
                VerificationItem.id.asc(),
            )
        )
        rows = list(self.session.scalars(stmt).all())
        pending = [row for row in rows if force or _verification_needs_recommendation_write(row)]
        if limit is not None:
            pending = pending[:limit]
        return pending

    def insert_if_new(
        self,
        *,
        verification_item_id: int,
        entity_id: int | None,
        title: str,
        summary_cn: str | None,
        why_recommend: str | None,
        how_to_try: str | None,
        risk_note: str | None,
        evidence_note: str | None,
        raw_response: dict | str | None = None,
        writer_version: str = "recommendation_writer_v1",
        source_verification_updated_at: datetime | None = None,
    ) -> InsertResult:
        stmt = select(RecommendationCard.id).where(RecommendationCard.verification_item_id == verification_item_id)
        if self.session.execute(stmt).first():
            return InsertResult(inserted=False, reason="duplicate_verification_item")
        raw_text = raw_response if isinstance(raw_response, str) else json.dumps(raw_response or {}, ensure_ascii=False, default=str)
        item = RecommendationCard(
            verification_item_id=verification_item_id,
            entity_id=entity_id,
            title=title,
            summary_cn=summary_cn,
            why_recommend=why_recommend,
            how_to_try=how_to_try,
            risk_note=risk_note,
            evidence_note=evidence_note,
            raw_response=raw_text,
            writer_version=writer_version,
            source_verification_updated_at=source_verification_updated_at,
            stale=False,
        )
        self.session.add(item)
        self.session.flush()
        return InsertResult(inserted=True, item_id=item.id)

    def upsert(
        self,
        *,
        verification_item_id: int,
        entity_id: int | None,
        title: str,
        summary_cn: str | None,
        why_recommend: str | None,
        how_to_try: str | None,
        risk_note: str | None,
        evidence_note: str | None,
        raw_response: dict | str | None = None,
        writer_version: str = "recommendation_writer_v1",
        source_verification_updated_at: datetime | None = None,
    ) -> InsertResult:
        item = self.session.scalars(
            select(RecommendationCard).where(RecommendationCard.verification_item_id == verification_item_id)
        ).first()
        created = False
        if item is None:
            item = RecommendationCard(verification_item_id=verification_item_id)
            self.session.add(item)
            created = True
        raw_text = raw_response if isinstance(raw_response, str) else json.dumps(raw_response or {}, ensure_ascii=False, default=str)
        now = datetime.now(timezone.utc)
        item.entity_id = entity_id
        item.title = title
        item.summary_cn = summary_cn
        item.why_recommend = why_recommend
        item.how_to_try = how_to_try
        item.risk_note = risk_note
        item.evidence_note = evidence_note
        item.raw_response = raw_text
        item.writer_version = writer_version
        item.source_verification_updated_at = source_verification_updated_at
        item.stale = False
        item.updated_at = now
        self.session.flush()
        return InsertResult(inserted=created, reason=None if created else "updated", item_id=item.id)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _max_datetime(values: list[datetime | None]) -> datetime | None:
    normalized = [_as_utc(value) for value in values if value is not None]
    normalized = [value for value in normalized if value is not None]
    return max(normalized) if normalized else None


def _source_evidence_updated_at(claim: ExtractedClaim) -> datetime | None:
    candidate = claim.candidate_item
    if candidate is None:
        return None
    return _max_datetime(
        [
            getattr(item, "updated_at", None)
            or getattr(item, "classified_at", None)
            or getattr(item, "fetched_at", None)
            for item in candidate.evidence_items
        ]
    )


def _source_claim_verification_updated_at(candidate: CandidateItem) -> datetime | None:
    rows = list(candidate.claim_verification_items or [])
    claim = getattr(candidate, "extracted_claim", None)
    if claim is not None:
        rows.extend(list(getattr(claim, "claim_verification_items", []) or []))
    unique = {row.id: row for row in rows if getattr(row, "id", None) is not None}
    rows = list(unique.values()) if unique else rows
    return _max_datetime([getattr(row, "updated_at", None) or getattr(row, "created_at", None) for row in rows])


def _claim_needs_verification(claim: ExtractedClaim) -> bool:
    rows = list(claim.claim_verification_items or [])
    if not rows:
        return True
    evidence_updated_at = _source_evidence_updated_at(claim)
    if evidence_updated_at is None:
        return False
    for row in rows:
        if getattr(row, "stale", False):
            return True
        source_time = _as_utc(getattr(row, "source_evidence_updated_at", None))
        if source_time is None or evidence_updated_at > source_time:
            row.stale = True
            return True
    return False


def _candidate_needs_ai_verification(candidate: CandidateItem) -> bool:
    verification = candidate.verification_item
    if verification is None:
        return True
    if getattr(verification, "stale", False):
        return True
    claim_updated_at = _source_claim_verification_updated_at(candidate)
    if claim_updated_at is None:
        return False
    source_time = _as_utc(getattr(verification, "source_claim_verification_updated_at", None))
    if source_time is None or claim_updated_at > source_time:
        verification.stale = True
        return True
    return False


def _verification_needs_recommendation_write(verification: VerificationItem) -> bool:
    card = verification.recommendation_card
    if card is None:
        return True
    if getattr(card, "stale", False):
        return True
    verification_updated_at = _as_utc(getattr(verification, "updated_at", None) or getattr(verification, "created_at", None))
    source_time = _as_utc(getattr(card, "source_verification_updated_at", None))
    if verification_updated_at is not None and (source_time is None or verification_updated_at > source_time):
        card.stale = True
        return True
    return False


def _clamp_score(value: int | float | None) -> int:
    if value is None:
        return 0
    return max(0, min(int(value), 100))


def _query_hash(query: str) -> str:
    return hashlib.sha256(" ".join(query.split()).lower().encode("utf-8")).hexdigest()


def _normalize_entity_name(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())[:255] or "unknown"


def _strong_key(claim: ExtractedClaim | None) -> str | None:
    if claim is None:
        return None
    return claim.github_url or claim.huggingface_url or claim.producthunt_url or claim.official_url


def _detect_entity_update(
    *,
    verification: VerificationItem,
    previous_last_seen: datetime | None,
    created_entity: bool,
) -> tuple[bool, str | None]:
    candidate = verification.candidate_item
    claim = candidate.extracted_claim
    reasons: list[str] = []
    if created_entity:
        reasons.append("new_entity")
    published_at = _as_utc(candidate.normalized_item.published_at if candidate.normalized_item else None)
    previous_seen = _as_utc(previous_last_seen)
    if previous_seen and published_at and published_at > previous_seen:
        reasons.append("new_mention")
    if claim and claim.release_signal:
        reasons.append("release_signal")
    claim_text = " ".join(_loads_json_list(claim.claims_json) if claim else []).lower()
    if any(keyword in claim_text for keyword in ["release", "released", "update", "updated", "version", "mcp", "gguf", "open weights", "开源", "发布", "更新"]):
        reasons.append("claim_update_keyword")
    if verification.freshness_score >= 80:
        reasons.append("fresh_verification")
    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        return False, None
    major = any(reason in reasons for reason in ["new_entity", "new_mention", "release_signal", "claim_update_keyword", "fresh_verification"])
    return major, ";".join(reasons)


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
