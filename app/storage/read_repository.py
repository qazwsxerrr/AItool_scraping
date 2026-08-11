"""Read-only DTO queries for the v2 web UI.

The repository is the only database boundary used by the web routes.  The
current pipeline stores one unified ``intel_items`` row with an optional
``ai_item_reviews`` row and an optional lightweight ``item_verifications``
row.  No removed claim/evidence/recommendation tables are queried here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.storage.models import AIItemReview, IntelItem, IntelItemVerification, IntelRun, Source


FINAL_ITEM_STATUSES = ("verified", "hotspot", "discovery_only")
PENDING_ITEM_STATUSES = ("new", "selected", "needs_review", "ai_failed")
ITEM_STATUSES = FINAL_ITEM_STATUSES + PENDING_ITEM_STATUSES + ("filtered", "rejected")
CONTENT_CLASSES = ("official_model_company", "project_tool", "community_social")


@dataclass(frozen=True)
class DashboardStats:
    raw_items: int
    selected_items: int
    hotspots: int
    verified_items: int
    discovery_items: int
    pending_items: int
    needs_review_items: int
    ai_failed_items: int
    filtered_items: int
    rejected_items: int
    last_run_type: str | None
    last_run_status: str | None
    last_run_started_at: datetime | None

    # These properties are kept for callers written against the pre-v2 UI
    # adapter.  They are aliases, not separate pipeline concepts.
    @property
    def candidates(self) -> int:
        # Historical callers used this as "not filtered/rejected/AI-failed";
        # keep that interpretation while the page uses ``selected_items``.
        return self.selected_items + self.pending_items - self.ai_failed_items

    @property
    def recommendations(self) -> int:
        return self.selected_items

    @property
    def kept_candidates(self) -> int:
        return self.candidates

    @property
    def stale_recommendations(self) -> int:
        return self.needs_review_items + self.ai_failed_items


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
    ai_keep: bool | None = None
    ai_status: str | None = None
    verification_status: str | None = None
    verification_url: str | None = None
    supports_basic_fact: bool = False

    @property
    def item_id(self) -> int:
        return self.id

    @property
    def status(self) -> str:
        return self.recommendation_level

    @property
    def content_class(self) -> str | None:
        return self.category

    @property
    def selection_score(self) -> int:
        return self.total_score

    @property
    def selection_reason(self) -> str | None:
        return self.why_recommend

    @property
    def ai_summary(self) -> str | None:
        return self.summary

    @property
    def ai_confidence(self) -> int:
        return self.credibility_score


# Canonical v2 name; the old class name remains an import-compatible alias.
FeaturedItemRow = FeaturedRecommendationRow


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
    content_class: str | None = None
    selection_reason: str | None = None
    ai_status: str | None = None
    verification_status: str | None = None
    verification_url: str | None = None
    supports_basic_fact: bool = False

    @property
    def item_id(self) -> int:
        return self.raw_item_id

    @property
    def status(self) -> str | None:
        return self.candidate_status

    @property
    def selection_score(self) -> int | None:
        return self.candidate_score

    @property
    def ai_confidence(self) -> int | None:
        return self.ai_score


@dataclass(frozen=True)
class AllItemFilters:
    query: str | None = None
    source_group: str | None = None
    status: str | None = None
    ai_keep: bool | None = None
    content_class: str | None = None


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
    content_class: str | None = None
    status: str | None = None
    ai_status: str | None = None
    verification_status: str | None = None
    selection_reason: str | None = None


@dataclass(frozen=True)
class SearchContentResults:
    query: str
    selected_items: list[SearchResultRow]
    items: list[SearchResultRow]

    @classmethod
    def empty(cls) -> "SearchContentResults":
        return cls(query="", selected_items=[], items=[])

    @property
    def recommendations(self) -> list[SearchResultRow]:
        """Compatibility alias for the pre-v2 selected group."""

        return self.selected_items

    @property
    def claims(self) -> list[SearchResultRow]:
        return []

    @property
    def evidence(self) -> list[SearchResultRow]:
        return []

    @property
    def total_count(self) -> int:
        return len(self.selected_items) + len(self.items)

    @property
    def all_items(self) -> list[SearchResultRow]:
        return self.items


@dataclass(frozen=True)
class UIFilterOptions:
    """Distinct v2 values available to the all-items filter form."""

    source_groups: tuple[str, ...]
    content_classes: tuple[str, ...]
    statuses: tuple[str, ...]


class UIReadRepository:
    """Stable read boundary backed only by the v2 tables."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_dashboard_stats(self) -> DashboardStats:
        last_run = self.session.scalars(
            select(IntelRun).order_by(IntelRun.started_at.desc(), IntelRun.id.desc()).limit(1)
        ).first()
        status_counts = {
            str(status): int(count)
            for status, count in self.session.execute(
                select(IntelItem.status, func.count()).group_by(IntelItem.status)
            ).all()
        }
        selected_items = sum(status_counts.get(status, 0) for status in FINAL_ITEM_STATUSES)
        pending_items = sum(status_counts.get(status, 0) for status in PENDING_ITEM_STATUSES)
        run_type = "run-once" if last_run else None
        if last_run and last_run.filters_json:
            try:
                filters = json.loads(last_run.filters_json)
            except (TypeError, json.JSONDecodeError):
                filters = {}
            if isinstance(filters, dict):
                run_type = str(filters.get("stage") or filters.get("command") or run_type)
        return DashboardStats(
            raw_items=self._count(IntelItem),
            selected_items=selected_items,
            hotspots=status_counts.get("hotspot", 0),
            verified_items=status_counts.get("verified", 0),
            discovery_items=status_counts.get("discovery_only", 0),
            pending_items=pending_items,
            needs_review_items=status_counts.get("needs_review", 0),
            ai_failed_items=status_counts.get("ai_failed", 0),
            filtered_items=status_counts.get("filtered", 0),
            rejected_items=status_counts.get("rejected", 0),
            last_run_type=run_type,
            last_run_status=last_run.status if last_run else None,
            last_run_started_at=last_run.started_at if last_run else None,
        )

    def list_filter_options(self) -> UIFilterOptions:
        """Return distinct filter values from persisted v2 rows.

        Source registry values are intentionally not hardcoded in the route;
        this keeps the UI correct when a deployment adds a source group or
        content class without requiring a template change.
        """

        source_groups = tuple(
            value
            for (value,) in self.session.execute(
                select(Source.source_group)
                .where(Source.source_group.is_not(None), Source.source_group != "")
                .distinct()
                .order_by(Source.source_group.asc())
            ).all()
            if value
        )
        persisted_content_classes = tuple(
            value
            for (value,) in self.session.execute(
                select(IntelItem.content_class)
                .where(IntelItem.content_class.is_not(None), IntelItem.content_class != "")
                .distinct()
                .order_by(IntelItem.content_class.asc())
            ).all()
            if value
        )
        content_classes = tuple(sorted(set(CONTENT_CLASSES).union(persisted_content_classes)))
        persisted_statuses = tuple(
            value
            for (value,) in self.session.execute(
                select(IntelItem.status)
                .where(IntelItem.status.is_not(None), IntelItem.status != "")
                .distinct()
                .order_by(IntelItem.status.asc())
            ).all()
            if value
        )
        statuses = tuple(sorted(set(ITEM_STATUSES).union(persisted_statuses)))
        return UIFilterOptions(
            source_groups=source_groups,
            content_classes=content_classes,
            statuses=statuses,
        )

    def list_featured_cards(
        self,
        *,
        category: str | None = None,
        direct_support_only: bool = False,
        hide_stale: bool = False,
        limit: int = 30,
    ) -> list[FeaturedRecommendationRow]:
        if limit <= 0:
            return []
        safe_limit = min(limit, 100)
        retained_without_ai = (IntelItem.content_class == "project_tool") & (IntelItem.status == "hotspot")
        retained_with_ai = AIItemReview.keep.is_(True) & IntelItem.status.in_(FINAL_ITEM_STATUSES)
        stmt = (
            select(IntelItem, Source, AIItemReview, IntelItemVerification)
            .join(Source, IntelItem.source_id == Source.id)
            .outerjoin(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .outerjoin(IntelItemVerification, IntelItemVerification.item_id == IntelItem.id)
            .where(retained_without_ai | retained_with_ai)
            .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc(), IntelItem.id.asc())
            .limit(max(safe_limit * 3, safe_limit))
        )
        if category:
            stmt = stmt.where(IntelItem.content_class == category)
        if hide_stale:
            stmt = stmt.where(
                IntelItem.status.not_in(["needs_review", "ai_failed"]),
                or_(AIItemReview.id.is_(None), AIItemReview.status != "ai_failed"),
            )

        rows: list[FeaturedRecommendationRow] = []
        for item, source, review, verification in self.session.execute(stmt).all():
            direct_count = 1 if verification and verification.supports_basic_fact else 0
            if direct_support_only and direct_count == 0:
                continue
            risk_flags = _json_list(review.risk_flags_json if review else None)
            if verification:
                risk_flags.extend(_json_list(verification.risk_flags_json))
            risk_flags = list(dict.fromkeys(risk_flags))
            rows.append(
                FeaturedRecommendationRow(
                    id=item.id,
                    candidate_item_id=item.id,
                    title=item.title,
                    summary=(review.summary_cn if review else None) or item.summary,
                    why_recommend=item.selection_reason or (review.reason if review else None),
                    how_to_try=_safe_url(item.canonical_url),
                    risk_note=("；".join(risk_flags) if risk_flags else None),
                    recommendation_level=item.status,
                    total_score=int(item.selection_score or 0),
                    credibility_score=_credibility(review, verification),
                    freshness_score=_freshness(item.published_at),
                    category=item.content_class,
                    source_name=source.name,
                    source_group=source.source_group,
                    source_subtype=source.source_subtype,
                    evidence_count=direct_count,
                    direct_support_count=direct_count,
                    risk_flags=risk_flags,
                    stale=item.status in {"needs_review", "ai_failed"} or bool(review and review.status == "ai_failed"),
                    published_at=item.published_at,
                    created_at=item.created_at,
                    ai_keep=review.keep if review else None,
                    ai_status=review.status if review else None,
                    verification_status=verification.status if verification else None,
                    verification_url=_safe_url(verification.verification_url) if verification else None,
                    supports_basic_fact=bool(verification and verification.supports_basic_fact),
                )
            )
            if len(rows) >= safe_limit:
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
            select(IntelItem, Source, AIItemReview, IntelItemVerification)
            .join(Source, IntelItem.source_id == Source.id)
            .outerjoin(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .outerjoin(IntelItemVerification, IntelItemVerification.item_id == IntelItem.id)
            .order_by(IntelItem.published_at.desc(), IntelItem.captured_at.desc(), IntelItem.id.desc())
            .offset((safe_page - 1) * safe_page_size)
            .limit(safe_page_size)
        )
        query = filters.query.strip() if filters.query else ""
        if query:
            like = f"%{query}%"
            stmt = stmt.where(
                or_(
                    IntelItem.title.ilike(like),
                    IntelItem.summary.ilike(like),
                    IntelItem.content_text.ilike(like),
                    AIItemReview.summary_cn.ilike(like),
                )
            )
        if filters.source_group:
            stmt = stmt.where(Source.source_group == filters.source_group.strip())
        if filters.content_class:
            stmt = stmt.where(IntelItem.content_class == filters.content_class.strip())
        if filters.status:
            stmt = stmt.where(IntelItem.status == filters.status.strip())
        if filters.ai_keep is not None:
            stmt = stmt.where(AIItemReview.keep.is_(filters.ai_keep))

        items: list[AllItemRow] = []
        for item, source, review, verification in self.session.execute(stmt).all():
            items.append(
                AllItemRow(
                    candidate_id=item.id,
                    raw_item_id=item.id,
                    normalized_item_id=item.id,
                    title=item.title,
                    url=_safe_url(item.canonical_url),
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
                    content_class=item.content_class,
                    selection_reason=item.selection_reason,
                    ai_status=review.status if review else None,
                    verification_status=verification.status if verification else None,
                    verification_url=_safe_url(verification.verification_url) if verification else None,
                    supports_basic_fact=bool(verification and verification.supports_basic_fact),
                )
            )
        return items

    def search_content(self, query: str, *, limit_per_group: int = 8) -> SearchContentResults:
        normalized_query = query.strip()
        if not normalized_query:
            return SearchContentResults.empty()
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
                content_class=row.content_class,
                status=row.candidate_status,
                ai_status=row.ai_status,
                verification_status=row.verification_status,
                selection_reason=row.selection_reason,
            )
            for row in self.list_all_items(filters=AllItemFilters(query=normalized_query), page_size=safe_limit)
        ]
        return SearchContentResults(
            query=normalized_query,
            selected_items=recommendations,
            items=items,
        )

    def _search_recommendations(self, *, like: str, limit: int) -> list[SearchResultRow]:
        retained_without_ai = (IntelItem.content_class == "project_tool") & (IntelItem.status == "hotspot")
        retained_with_ai = AIItemReview.keep.is_(True) & IntelItem.status.in_(FINAL_ITEM_STATUSES)
        stmt = (
            select(IntelItem, Source, AIItemReview)
            .join(Source, IntelItem.source_id == Source.id)
            .outerjoin(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .where(retained_without_ai | retained_with_ai)
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
                    summary=(review.summary_cn if review else None) or item.summary,
                    url=_safe_url(item.canonical_url),
                    source_name=source.name,
                    candidate_item_id=item.id,
                    score=item.selection_score,
                    badges=[badge for badge in ["selected", item.content_class, item.status] if badge],
                    published_at=item.published_at,
                    created_at=item.created_at,
                    content_class=item.content_class,
                    status=item.status,
                    ai_status=review.status if review else None,
                    selection_reason=item.selection_reason or (review.reason if review else None),
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


def _safe_url(value: str | None) -> str | None:
    """Expose only navigable HTTP(S) links to templates."""

    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _credibility(review: AIItemReview | None, verification: IntelItemVerification | None) -> int:
    if verification and verification.supports_basic_fact:
        return 100
    return max(0, min(int(review.confidence if review else 0), 100))


def _freshness(value: datetime | None) -> int:
    if value is None:
        return 0
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - aware.astimezone(timezone.utc)).total_seconds() / 86400)
    return max(0, min(100, round(100 - age_days * 3)))
