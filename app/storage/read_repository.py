from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.storage.models import (
    AIReviewItem,
    CandidateItem,
    ClaimVerificationItem,
    EvidenceItem,
    ExtractedClaim,
    NormalizedItem,
    PipelineRun,
    RawItem,
    RecommendationCard,
    Source,
    VerificationItem,
)


@dataclass(frozen=True)
class DashboardStats:
    raw_items: int
    candidates: int
    recommendations: int
    kept_candidates: int
    stale_recommendations: int
    last_run_type: str | None
    last_run_status: str | None
    last_run_started_at: datetime | None


@dataclass(frozen=True)
class FeaturedRecommendationRow:
    id: int
    candidate_item_id: int
    title: str
    summary: str | None
    why_recommend: str | None
    how_to_try: str | None
    risk_note: str | None
    recommendation_level: str
    total_score: int
    credibility_score: int
    freshness_score: int
    category: str | None
    source_name: str | None
    source_group: str | None
    source_subtype: str | None
    evidence_count: int
    direct_support_count: int
    risk_flags: list[str]
    stale: bool
    published_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class AllItemRow:
    candidate_id: int | None
    raw_item_id: int
    normalized_item_id: int | None
    title: str
    url: str | None
    source_name: str
    source_group: str | None
    source_subtype: str | None
    source_role: str | None
    spam_risk: str | None
    candidate_score: int | None
    candidate_status: str | None
    ai_score: int | None
    ai_keep: bool | None
    summary_cn: str | None
    published_at: datetime | None
    fetched_at: datetime | None


@dataclass(frozen=True)
class AllItemFilters:
    query: str | None = None
    source_group: str | None = None
    status: str | None = None
    ai_keep: bool | None = None


@dataclass(frozen=True)
class SearchResultRow:
    result_type: str
    id: int
    title: str
    summary: str | None
    url: str | None
    source_name: str | None
    candidate_item_id: int | None
    score: int | None
    badges: list[str]
    published_at: datetime | None
    created_at: datetime | None


@dataclass(frozen=True)
class SearchContentResults:
    query: str
    recommendations: list[SearchResultRow]
    items: list[SearchResultRow]
    claims: list[SearchResultRow]
    evidence: list[SearchResultRow]

    @property
    def total_count(self) -> int:
        return len(self.recommendations) + len(self.items) + len(self.claims) + len(self.evidence)


class UIReadRepository:
    """Read-only query layer for the local web UI.

    Route handlers should depend on this class instead of knowing the storage
    schema.  It returns small DTOs with JSON fields already decoded safely.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_dashboard_stats(self) -> DashboardStats:
        last_run = self.session.scalars(
            select(PipelineRun).order_by(PipelineRun.started_at.desc(), PipelineRun.id.desc()).limit(1)
        ).first()
        return DashboardStats(
            raw_items=self._count(RawItem),
            candidates=self._count(CandidateItem),
            recommendations=self._count(RecommendationCard),
            kept_candidates=self._count(CandidateItem, CandidateItem.status == "kept"),
            stale_recommendations=self._count(RecommendationCard, RecommendationCard.stale.is_(True)),
            last_run_type=last_run.run_type if last_run else None,
            last_run_status=last_run.status if last_run else None,
            last_run_started_at=last_run.started_at if last_run else None,
        )

    def list_featured_cards(
        self,
        *,
        category: str | None = None,
        direct_support_only: bool = False,
        hide_stale: bool = False,
        limit: int = 30,
    ) -> list[FeaturedRecommendationRow]:
        stmt = (
            select(RecommendationCard, VerificationItem, CandidateItem, NormalizedItem, RawItem, Source)
            .join(VerificationItem, RecommendationCard.verification_item_id == VerificationItem.id)
            .join(CandidateItem, VerificationItem.candidate_item_id == CandidateItem.id)
            .join(NormalizedItem, CandidateItem.normalized_item_id == NormalizedItem.id)
            .join(RawItem, NormalizedItem.raw_item_id == RawItem.id)
            .join(Source, RawItem.source_id == Source.id)
            .where(VerificationItem.final_keep.is_(True))
            .order_by(
                RecommendationCard.stale.asc(),
                VerificationItem.final_score.desc(),
                RecommendationCard.created_at.desc(),
            )
            .limit(max(limit * 3, limit))
        )
        if category:
            stmt = stmt.where(VerificationItem.category == category)
        if hide_stale:
            stmt = stmt.where(RecommendationCard.stale.is_(False), VerificationItem.stale.is_(False))

        rows: list[FeaturedRecommendationRow] = []
        for card, verification, candidate, normalized, raw, source in self.session.execute(stmt).all():
            evidence_count, direct_support_count = self._support_counts(candidate.id)
            if direct_support_only and direct_support_count <= 0:
                continue
            rows.append(
                FeaturedRecommendationRow(
                    id=card.id,
                    candidate_item_id=candidate.id,
                    title=card.title,
                    summary=card.summary_cn or verification.summary_cn,
                    why_recommend=card.why_recommend,
                    how_to_try=card.how_to_try,
                    risk_note=card.risk_note or verification.risk_reason,
                    recommendation_level=verification.recommendation_level,
                    total_score=verification.final_score,
                    credibility_score=verification.credibility_score,
                    freshness_score=verification.freshness_score,
                    category=verification.category,
                    source_name=source.name,
                    source_group=candidate.source_group or source.source_group,
                    source_subtype=candidate.source_subtype or source.source_subtype,
                    evidence_count=evidence_count,
                    direct_support_count=direct_support_count,
                    risk_flags=_json_list(verification.risk_flags),
                    stale=bool(card.stale or verification.stale),
                    published_at=normalized.published_at or raw.published_at,
                    created_at=card.created_at,
                )
            )
            if len(rows) >= limit:
                break
        return rows

    def list_all_items(
        self,
        *,
        filters: AllItemFilters | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> list[AllItemRow]:
        filters = filters or AllItemFilters()
        safe_page = max(page, 1)
        safe_page_size = min(max(page_size, 1), 100)
        stmt = (
            select(RawItem, Source, NormalizedItem, CandidateItem, AIReviewItem)
            .join(Source, RawItem.source_id == Source.id)
            .outerjoin(NormalizedItem, NormalizedItem.raw_item_id == RawItem.id)
            .outerjoin(CandidateItem, CandidateItem.normalized_item_id == NormalizedItem.id)
            .outerjoin(AIReviewItem, AIReviewItem.candidate_item_id == CandidateItem.id)
            .order_by(RawItem.published_at.desc(), RawItem.fetched_at.desc(), RawItem.id.desc())
            .offset((safe_page - 1) * safe_page_size)
            .limit(safe_page_size)
        )

        if filters.query:
            like = f"%{filters.query.strip()}%"
            stmt = stmt.where(
                or_(
                    RawItem.title.ilike(like),
                    RawItem.raw_summary.ilike(like),
                    NormalizedItem.title.ilike(like),
                    NormalizedItem.body_text.ilike(like),
                    AIReviewItem.summary_cn.ilike(like),
                )
            )
        if filters.source_group:
            stmt = stmt.where(or_(Source.source_group == filters.source_group, CandidateItem.source_group == filters.source_group))
        if filters.status:
            stmt = stmt.where(CandidateItem.status == filters.status)
        if filters.ai_keep is not None:
            stmt = stmt.where(AIReviewItem.ai_keep.is_(filters.ai_keep))

        items: list[AllItemRow] = []
        for raw, source, normalized, candidate, ai_review in self.session.execute(stmt).all():
            items.append(
                AllItemRow(
                    candidate_id=candidate.id if candidate else None,
                    raw_item_id=raw.id,
                    normalized_item_id=normalized.id if normalized else None,
                    title=(normalized.title if normalized else raw.title),
                    url=(normalized.url if normalized and normalized.url else raw.link),
                    source_name=source.name,
                    source_group=(candidate.source_group if candidate else source.source_group),
                    source_subtype=(candidate.source_subtype if candidate else source.source_subtype),
                    source_role=source.source_role,
                    spam_risk=source.spam_risk,
                    candidate_score=candidate.candidate_score if candidate else None,
                    candidate_status=candidate.status if candidate else None,
                    ai_score=ai_review.ai_score if ai_review else None,
                    ai_keep=ai_review.ai_keep if ai_review else None,
                    summary_cn=ai_review.summary_cn if ai_review else raw.raw_summary,
                    published_at=(normalized.published_at if normalized else raw.published_at),
                    fetched_at=raw.fetched_at,
                )
            )
        return items

    def search_content(self, query: str, *, limit_per_group: int = 8) -> SearchContentResults:
        normalized_query = query.strip()
        if not normalized_query:
            return SearchContentResults(query="", recommendations=[], items=[], claims=[], evidence=[])

        safe_limit = min(max(limit_per_group, 1), 20)
        like = f"%{normalized_query}%"
        return SearchContentResults(
            query=normalized_query,
            recommendations=self._search_recommendations(like=like, limit=safe_limit),
            items=self._search_items(query=normalized_query, limit=safe_limit),
            claims=self._search_claims(like=like, limit=safe_limit),
            evidence=self._search_evidence(like=like, limit=safe_limit),
        )

    def _search_recommendations(self, *, like: str, limit: int) -> list[SearchResultRow]:
        stmt = (
            select(RecommendationCard, VerificationItem, CandidateItem, NormalizedItem, RawItem, Source)
            .join(VerificationItem, RecommendationCard.verification_item_id == VerificationItem.id)
            .join(CandidateItem, VerificationItem.candidate_item_id == CandidateItem.id)
            .join(NormalizedItem, CandidateItem.normalized_item_id == NormalizedItem.id)
            .join(RawItem, NormalizedItem.raw_item_id == RawItem.id)
            .join(Source, RawItem.source_id == Source.id)
            .where(
                or_(
                    RecommendationCard.title.ilike(like),
                    RecommendationCard.summary_cn.ilike(like),
                    RecommendationCard.why_recommend.ilike(like),
                    RecommendationCard.how_to_try.ilike(like),
                    VerificationItem.summary_cn.ilike(like),
                    VerificationItem.category.ilike(like),
                )
            )
            .order_by(VerificationItem.final_score.desc(), RecommendationCard.created_at.desc())
            .limit(limit)
        )
        rows: list[SearchResultRow] = []
        for card, verification, candidate, normalized, raw, source in self.session.execute(stmt).all():
            badges = ["recommendation", verification.recommendation_level]
            if verification.category:
                badges.append(verification.category)
            if card.stale or verification.stale:
                badges.append("stale")
            rows.append(
                SearchResultRow(
                    result_type="recommendation",
                    id=card.id,
                    title=card.title,
                    summary=card.summary_cn or verification.summary_cn,
                    url=normalized.url or raw.link,
                    source_name=source.name,
                    candidate_item_id=candidate.id,
                    score=verification.final_score,
                    badges=badges,
                    published_at=normalized.published_at or raw.published_at,
                    created_at=card.created_at,
                )
            )
        return rows

    def _search_items(self, *, query: str, limit: int) -> list[SearchResultRow]:
        items = self.list_all_items(filters=AllItemFilters(query=query), page=1, page_size=limit)
        return [
            SearchResultRow(
                result_type="item",
                id=item.raw_item_id,
                title=item.title,
                summary=item.summary_cn,
                url=item.url,
                source_name=item.source_name,
                candidate_item_id=item.candidate_id,
                score=item.ai_score or item.candidate_score,
                badges=[badge for badge in ["item", item.source_group, item.candidate_status] if badge],
                published_at=item.published_at,
                created_at=item.fetched_at,
            )
            for item in items
        ]

    def _search_claims(self, *, like: str, limit: int) -> list[SearchResultRow]:
        stmt = (
            select(ExtractedClaim, CandidateItem, NormalizedItem, RawItem, Source)
            .join(CandidateItem, ExtractedClaim.candidate_item_id == CandidateItem.id)
            .join(NormalizedItem, CandidateItem.normalized_item_id == NormalizedItem.id)
            .join(RawItem, NormalizedItem.raw_item_id == RawItem.id)
            .join(Source, RawItem.source_id == Source.id)
            .where(
                or_(
                    ExtractedClaim.entity_name.ilike(like),
                    ExtractedClaim.entity_type.ilike(like),
                    ExtractedClaim.claims_json.ilike(like),
                    ExtractedClaim.official_url.ilike(like),
                    ExtractedClaim.github_url.ilike(like),
                    NormalizedItem.title.ilike(like),
                )
            )
            .order_by(ExtractedClaim.confidence.desc(), ExtractedClaim.created_at.desc())
            .limit(limit)
        )
        rows: list[SearchResultRow] = []
        for claim, candidate, normalized, raw, source in self.session.execute(stmt).all():
            title = claim.entity_name or normalized.title
            rows.append(
                SearchResultRow(
                    result_type="claim",
                    id=claim.id,
                    title=title,
                    summary=_claim_summary(claim.claims_json),
                    url=claim.github_url or claim.official_url or normalized.url or raw.link,
                    source_name=source.name,
                    candidate_item_id=candidate.id,
                    score=claim.confidence,
                    badges=[badge for badge in ["claim", claim.entity_type, claim.evidence_status] if badge],
                    published_at=normalized.published_at or raw.published_at,
                    created_at=claim.created_at,
                )
            )
        return rows

    def _search_evidence(self, *, like: str, limit: int) -> list[SearchResultRow]:
        stmt = (
            select(EvidenceItem, CandidateItem, NormalizedItem, RawItem, Source)
            .join(CandidateItem, EvidenceItem.candidate_item_id == CandidateItem.id)
            .join(NormalizedItem, CandidateItem.normalized_item_id == NormalizedItem.id)
            .join(RawItem, NormalizedItem.raw_item_id == RawItem.id)
            .join(Source, RawItem.source_id == Source.id)
            .where(
                or_(
                    EvidenceItem.url.ilike(like),
                    EvidenceItem.title.ilike(like),
                    EvidenceItem.snippet.ilike(like),
                    EvidenceItem.source_domain.ilike(like),
                    EvidenceItem.fetched_title.ilike(like),
                    EvidenceItem.fetched_description.ilike(like),
                    EvidenceItem.fetched_text_preview.ilike(like),
                )
            )
            .order_by(EvidenceItem.evidence_confidence.desc(), EvidenceItem.fetched_at.desc())
            .limit(limit)
        )
        rows: list[SearchResultRow] = []
        for evidence, candidate, normalized, raw, source in self.session.execute(stmt).all():
            rows.append(
                SearchResultRow(
                    result_type="evidence",
                    id=evidence.id,
                    title=evidence.title or evidence.fetched_title or evidence.url,
                    summary=evidence.snippet or evidence.fetched_description or evidence.fetched_text_preview,
                    url=evidence.final_url or evidence.url,
                    source_name=source.name,
                    candidate_item_id=candidate.id,
                    score=evidence.evidence_confidence or evidence.confidence,
                    badges=[
                        badge
                        for badge in [
                            "evidence",
                            evidence.evidence_type,
                            evidence.supports_claim,
                            evidence.fetch_status,
                            evidence.classify_status,
                        ]
                        if badge
                    ],
                    published_at=normalized.published_at or raw.published_at,
                    created_at=evidence.fetched_at,
                )
            )
        return rows

    def _support_counts(self, candidate_item_id: int) -> tuple[int, int]:
        evidence_count = self._count(EvidenceItem, EvidenceItem.candidate_item_id == candidate_item_id)
        direct_support_count = self._count(
            ClaimVerificationItem,
            ClaimVerificationItem.candidate_item_id == candidate_item_id,
            ClaimVerificationItem.support_strength.in_(["direct_support", "direct", "strong"]),
        )
        if direct_support_count == 0:
            direct_support_count = self._count(
                EvidenceItem,
                EvidenceItem.candidate_item_id == candidate_item_id,
                EvidenceItem.supports_claim == "support",
            )
        return evidence_count, direct_support_count

    def _count(self, model: Any, *conditions: Any) -> int:
        stmt = select(func.count()).select_from(model)
        if conditions:
            stmt = stmt.where(*conditions)
        return int(self.session.execute(stmt).scalar_one())


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _claim_summary(raw: str | None) -> str | None:
    claims = _json_list(raw)
    if claims:
        return "；".join(claims[:3])
    return _trim(raw, 180)


def _trim(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
