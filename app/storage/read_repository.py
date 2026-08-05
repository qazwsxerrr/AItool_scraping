"""Read-only DTO queries for the existing web UI.

The UI routes and templates are intentionally unchanged. This adapter maps the
compact v2 records to the DTO names already consumed by those templates; it
does not recreate the removed claim/evidence/recommendation pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.storage.models import AIItemReview, IntelItem, IntelItemVerification, IntelRun, Source


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
    """Stable read boundary backed only by the v2 tables."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_dashboard_stats(self) -> DashboardStats:
        last_run = self.session.scalars(
            select(IntelRun).order_by(IntelRun.started_at.desc(), IntelRun.id.desc()).limit(1)
        ).first()
        recommendation_filter = IntelItem.status.in_(["verified", "hotspot", "discovery_only"])
        kept_filter = IntelItem.status.not_in(["filtered", "rejected", "ai_failed"])
        return DashboardStats(
            raw_items=self._count(IntelItem),
            candidates=self._count(IntelItem, kept_filter),
            recommendations=self._count(IntelItem, recommendation_filter),
            kept_candidates=self._count(IntelItem, kept_filter),
            stale_recommendations=self._count(IntelItem, IntelItem.status.in_(["needs_review", "ai_failed"])),
            last_run_type="run-once" if last_run else None,
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
            select(IntelItem, Source, AIItemReview, IntelItemVerification)
            .join(Source, IntelItem.source_id == Source.id)
            .join(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .outerjoin(IntelItemVerification, IntelItemVerification.item_id == IntelItem.id)
            .where(AIItemReview.keep.is_(True))
            .where(IntelItem.status.in_(["verified", "hotspot", "discovery_only"]))
            .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc(), IntelItem.id.asc())
            .limit(max(limit * 3, limit))
        )
        if category:
            stmt = stmt.where(AIItemReview.content_class == category)
        if hide_stale:
            stmt = stmt.where(IntelItem.status.not_in(["needs_review", "ai_failed"]))

        rows: list[FeaturedRecommendationRow] = []
        for item, source, review, verification in self.session.execute(stmt).all():
            direct_count = 1 if verification and verification.supports_basic_fact else 0
            if direct_support_only and direct_count == 0:
                continue
            risk_flags = _json_list(review.risk_flags_json)
            if verification:
                risk_flags.extend(_json_list(verification.risk_flags_json))
            risk_flags = list(dict.fromkeys(risk_flags))
            rows.append(
                FeaturedRecommendationRow(
                    id=item.id,
                    candidate_item_id=item.id,
                    title=item.title,
                    summary=review.summary_cn or item.summary,
                    why_recommend=review.reason,
                    how_to_try=item.canonical_url,
                    risk_note=("；".join(risk_flags) if risk_flags else None),
                    recommendation_level=item.status,
                    total_score=int(item.selection_score or 0),
                    credibility_score=_credibility(review, verification),
                    freshness_score=_freshness(item.published_at),
                    category=review.content_class,
                    source_name=source.name,
                    source_group=source.source_group,
                    source_subtype=source.source_subtype,
                    evidence_count=direct_count,
                    direct_support_count=direct_count,
                    risk_flags=risk_flags,
                    stale=item.status in {"needs_review", "ai_failed"},
                    published_at=item.published_at,
                    created_at=item.created_at,
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
            select(IntelItem, Source, AIItemReview)
            .join(Source, IntelItem.source_id == Source.id)
            .outerjoin(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .order_by(IntelItem.published_at.desc(), IntelItem.captured_at.desc(), IntelItem.id.desc())
            .offset((safe_page - 1) * safe_page_size)
            .limit(safe_page_size)
        )
        if filters.query:
            like = f"%{filters.query.strip()}%"
            stmt = stmt.where(
                or_(
                    IntelItem.title.ilike(like),
                    IntelItem.summary.ilike(like),
                    IntelItem.content_text.ilike(like),
                    AIItemReview.summary_cn.ilike(like),
                )
            )
        if filters.source_group:
            stmt = stmt.where(Source.source_group == filters.source_group)
        if filters.status:
            stmt = stmt.where(IntelItem.status == filters.status)
        if filters.ai_keep is not None:
            stmt = stmt.where(AIItemReview.keep.is_(filters.ai_keep))

        items: list[AllItemRow] = []
        for item, source, review in self.session.execute(stmt).all():
            items.append(
                AllItemRow(
                    candidate_id=item.id,
                    raw_item_id=item.id,
                    normalized_item_id=item.id,
                    title=item.title,
                    url=item.canonical_url,
                    source_name=source.name,
                    source_group=source.source_group,
                    source_subtype=source.source_subtype,
                    source_role=source.source_role,
                    spam_risk=source.spam_risk,
                    candidate_score=item.selection_score,
                    candidate_status=item.status,
                    ai_score=review.confidence if review else None,
                    ai_keep=review.keep if review else None,
                    summary_cn=(review.summary_cn if review else None) or item.summary,
                    published_at=item.published_at,
                    fetched_at=item.captured_at,
                )
            )
        return items

    def search_content(self, query: str, *, limit_per_group: int = 8) -> SearchContentResults:
        normalized_query = query.strip()
        if not normalized_query:
            return SearchContentResults(query="", recommendations=[], items=[], claims=[], evidence=[])
        safe_limit = min(max(limit_per_group, 1), 20)
        like = f"%{normalized_query}%"
        recommendations = self._search_recommendations(like=like, limit=safe_limit)
        items = [
            SearchResultRow(
                result_type="item",
                id=row.raw_item_id,
                title=row.title,
                summary=row.summary_cn,
                url=row.url,
                source_name=row.source_name,
                candidate_item_id=row.candidate_id,
                score=row.ai_score if row.ai_score is not None else row.candidate_score,
                badges=[badge for badge in ["item", row.source_group, row.candidate_status] if badge],
                published_at=row.published_at,
                created_at=row.fetched_at,
            )
            for row in self.list_all_items(filters=AllItemFilters(query=normalized_query), page_size=safe_limit)
        ]
        return SearchContentResults(
            query=normalized_query,
            recommendations=recommendations,
            items=items,
            claims=[],
            evidence=[],
        )

    def _search_recommendations(self, *, like: str, limit: int) -> list[SearchResultRow]:
        stmt = (
            select(IntelItem, Source, AIItemReview)
            .join(Source, IntelItem.source_id == Source.id)
            .join(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .where(AIItemReview.keep.is_(True))
            .where(IntelItem.status.in_(["verified", "hotspot", "discovery_only"]))
            .where(
                or_(
                    IntelItem.title.ilike(like),
                    IntelItem.summary.ilike(like),
                    IntelItem.content_text.ilike(like),
                    AIItemReview.summary_cn.ilike(like),
                    AIItemReview.reason.ilike(like),
                )
            )
            .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc())
            .limit(limit)
        )
        rows: list[SearchResultRow] = []
        for item, source, review in self.session.execute(stmt).all():
            rows.append(
                SearchResultRow(
                    result_type="recommendation",
                    id=item.id,
                    title=item.title,
                    summary=review.summary_cn or item.summary,
                    url=item.canonical_url,
                    source_name=source.name,
                    candidate_item_id=item.id,
                    score=item.selection_score,
                    badges=["recommendation", review.content_class, item.status],
                    published_at=item.published_at,
                    created_at=item.created_at,
                )
            )
        return rows

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


def _credibility(review: AIItemReview, verification: IntelItemVerification | None) -> int:
    if verification and verification.supports_basic_fact:
        return 100
    return max(0, min(int(review.confidence or 0), 100))


def _freshness(value: datetime | None) -> int:
    if value is None:
        return 0
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - aware.astimezone(timezone.utc)).total_seconds() / 86400)
    return max(0, min(100, round(100 - age_days * 3)))
