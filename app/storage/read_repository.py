"""Read-only DTO queries for the AI-only web UI.

The repository is the only database boundary used by web routes. It reads
unified items, their structured AI review, and source attribution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app.storage.models import (
    AIItemReview,
    IntelEvent,
    IntelEventRankingSnapshot,
    IntelItem,
    IntelRun,
    Source,
)


FINAL_ITEM_STATUSES = ("selected",)
PENDING_ITEM_STATUSES = ("new", "selected", "ai_failed")
DASHBOARD_PENDING_ITEM_STATUSES = ("new", "ai_failed")
ITEM_STATUSES = FINAL_ITEM_STATUSES + PENDING_ITEM_STATUSES + ("filtered", "rejected")
CONTENT_CLASSES = ("official_model_company", "project_tool", "community_social")


@dataclass(frozen=True)
class DashboardStats:
    raw_items: int
    selected_items: int
    pending_items: int
    ai_failed_items: int
    filtered_items: int
    rejected_items: int
    last_run_type: str | None
    last_run_status: str | None
    last_run_started_at: datetime | None

@dataclass(frozen=True)
class FeaturedItemRow:
    id: int
    title: str
    summary: str | None
    selection_reason: str | None
    url: str | None
    risk_note: str | None
    status: str
    selection_score: int
    ai_confidence: int
    content_class: str | None
    source_name: str | None
    source_group: str | None
    source_subtype: str | None
    risk_flags: list[str]
    published_at: datetime | None
    created_at: datetime | None
    ai_keep: bool | None = None
    ai_status: str | None = None


@dataclass(frozen=True)
class FeaturedEventRow:
    """Event-level card backed by the selected editorial snapshot."""

    event_id: int
    rank: int
    title: str
    summary: str | None
    url: str | None
    display_score: float
    topic: str | None
    content_class: str | None
    source_name: str | None
    source_group: str | None
    source_subtype: str | None
    source_ids: tuple[str, ...]
    risk_flags: list[str]
    published_at: datetime | None

    @property
    def id(self) -> int:
        """Compatibility alias for templates/card consumers."""

        return self.event_id


@dataclass(frozen=True)
class AllItemRow:
    item_id: int
    title: str
    url: str | None
    source_name: str
    source_group: str | None
    source_subtype: str | None
    source_role: str | None
    spam_risk: str | None
    selection_score: int | None
    status: str | None
    ai_confidence: int | None
    ai_keep: bool | None
    summary_cn: str | None
    published_at: datetime | None
    fetched_at: datetime | None
    content_class: str | None = None
    selection_reason: str | None = None
    ai_status: str | None = None

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
    item_id: int | None
    score: int | None
    badges: list[str]
    published_at: datetime | None
    created_at: datetime | None
    content_class: str | None = None
    status: str | None = None
    ai_status: str | None = None
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
    def total_count(self) -> int:
        return len(self.selected_items) + len(self.items)


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
        selected_items = int(
            self.session.execute(
                select(func.count())
                .select_from(IntelItem)
                .join(AIItemReview, AIItemReview.item_id == IntelItem.id)
                .where(
                    IntelItem.status == "selected",
                    AIItemReview.status == "success",
                    AIItemReview.keep.is_(True),
                )
            ).scalar_one()
        )
        pending_items = sum(status_counts.get(status, 0) for status in DASHBOARD_PENDING_ITEM_STATUSES)
        pending_items += int(
            self.session.execute(
                select(func.count())
                .select_from(IntelItem)
                .outerjoin(AIItemReview, AIItemReview.item_id == IntelItem.id)
                .where(
                    IntelItem.status == "selected",
                    or_(AIItemReview.id.is_(None), AIItemReview.status != "success"),
                )
            ).scalar_one()
        )
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
            pending_items=pending_items,
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
        statuses = tuple(sorted(set(ITEM_STATUSES).union(set(persisted_statuses) - {"hotspot"})))
        return UIFilterOptions(
            source_groups=source_groups,
            content_classes=content_classes,
            statuses=statuses,
        )

    def list_featured_cards(
        self,
        *,
        category: str | None = None,
        limit: int = 30,
    ) -> list[FeaturedItemRow]:
        if limit <= 0:
            return []
        safe_limit = min(limit, 100)
        # Wave 3 makes the editorial snapshot the homepage source of truth.
        # Keep the item query as a compatibility fallback for databases created
        # before the event/ranking tables existed or before the first ranking
        # run.  Once any ``latest`` snapshot row exists, an intentionally empty
        # selected set remains empty (no padding from raw item scores).
        snapshot_exists = self.session.execute(
            select(func.count())
            .select_from(IntelEventRankingSnapshot)
            .where(IntelEventRankingSnapshot.snapshot_key == "latest")
        ).scalar_one()
        if int(snapshot_exists or 0) > 0:
            return self._list_featured_event_cards(category=category, limit=safe_limit)
        stmt = (
            select(IntelItem, Source, AIItemReview)
            .join(Source, IntelItem.source_id == Source.id)
            .join(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .where(
                IntelItem.status == "selected",
                AIItemReview.status == "success",
                AIItemReview.keep.is_(True),
            )
            .order_by(IntelItem.selection_score.desc(), IntelItem.published_at.desc(), IntelItem.id.asc())
            .limit(max(safe_limit * 3, safe_limit))
        )
        if category:
            stmt = stmt.where(IntelItem.content_class == category)
        rows: list[FeaturedItemRow] = []
        for item, source, review in self.session.execute(stmt).all():
            risk_flags = _json_list(review.risk_flags_json if review else None)
            risk_flags = list(dict.fromkeys(risk_flags))
            rows.append(
                FeaturedItemRow(
                    id=item.id,
                    title=item.title,
                    summary=(review.summary_cn if review else None) or item.summary,
                    selection_reason=item.selection_reason or (review.reason if review else None),
                    url=_safe_url(item.canonical_url),
                    risk_note=("；".join(risk_flags) if risk_flags else None),
                    status=item.status,
                    selection_score=int(item.selection_score or 0),
                    ai_confidence=_credibility(review),
                    content_class=item.content_class,
                    source_name=source.name,
                    source_group=source.source_group,
                    source_subtype=source.source_subtype,
                    risk_flags=risk_flags,
                    published_at=item.published_at,
                    created_at=item.created_at,
                    ai_keep=review.keep if review else None,
                    ai_status=review.status if review else None,
                )
            )
            if len(rows) >= safe_limit:
                break
        return rows

    def list_featured_events(
        self,
        *,
        snapshot_key: str = "latest",
        topic: str | None = None,
        content_class: str | None = None,
        limit: int = 30,
    ) -> list[FeaturedEventRow]:
        """Read selected event cards from one immutable ranking snapshot."""

        if limit <= 0:
            return []
        stmt = (
            select(IntelEventRankingSnapshot, IntelEvent)
            .join(IntelEvent, IntelEvent.id == IntelEventRankingSnapshot.event_id)
            .where(
                IntelEventRankingSnapshot.snapshot_key == snapshot_key,
                IntelEventRankingSnapshot.selected.is_(True),
            )
            .order_by(IntelEventRankingSnapshot.rank.asc(), IntelEvent.id.asc())
            .limit(min(limit, 100))
        )
        if topic:
            stmt = stmt.where(IntelEventRankingSnapshot.topic == topic)
        if content_class:
            stmt = stmt.where(IntelEventRankingSnapshot.content_class == content_class)
        rows: list[FeaturedEventRow] = []
        for snapshot, event in self.session.execute(stmt).all():
            rows.append(self._event_row(snapshot, event))
        return rows

    # Descriptive aliases used by route/integration callers.
    list_homepage_events = list_featured_events
    list_ranking_snapshot = list_featured_events

    def _list_featured_event_cards(
        self,
        *,
        category: str | None,
        limit: int,
    ) -> list[FeaturedItemRow]:
        stmt = (
            select(IntelEventRankingSnapshot, IntelEvent)
            .join(IntelEvent, IntelEvent.id == IntelEventRankingSnapshot.event_id)
            .where(
                IntelEventRankingSnapshot.snapshot_key == "latest",
                IntelEventRankingSnapshot.selected.is_(True),
            )
            .order_by(IntelEventRankingSnapshot.rank.asc(), IntelEvent.id.asc())
            .limit(min(max(limit * 3, limit), 100))
        )
        if category:
            stmt = stmt.where(IntelEventRankingSnapshot.content_class == category)
        cards: list[FeaturedItemRow] = []
        for snapshot, event in self.session.execute(stmt).all():
            row = self._event_row(snapshot, event)
            cards.append(
                FeaturedItemRow(
                    id=row.event_id,
                    title=row.title,
                    summary=row.summary,
                    selection_reason=f"editorial_rank:{row.rank}",
                    url=row.url,
                    risk_note=("；".join(row.risk_flags) if row.risk_flags else None),
                    status="selected",
                    selection_score=int(round(row.display_score)),
                    ai_confidence=100,
                    content_class=row.content_class,
                    source_name=row.source_name,
                    source_group=row.source_group,
                    source_subtype=row.source_subtype,
                    risk_flags=row.risk_flags,
                    published_at=row.published_at,
                    created_at=event.created_at,
                    ai_keep=True,
                    ai_status="editorial_snapshot",
                )
            )
            if len(cards) >= limit:
                break
        return cards

    def _event_row(
        self,
        snapshot: IntelEventRankingSnapshot,
        event: IntelEvent,
    ) -> FeaturedEventRow:
        source_ids = _json_list(event.source_ids_json)
        source_names: list[str] = []
        source_groups: list[str] = []
        source_subtype: str | None = None
        for relation in event.event_items:
            source = relation.source or (relation.item.source if relation.item is not None else None)
            if source is not None:
                source_subtype = source_subtype or source.source_subtype
                if source.name and source.name not in source_names:
                    source_names.append(source.name)
                if source.source_group and source.source_group not in source_groups:
                    source_groups.append(source.source_group)
            if relation.source_id and relation.source_id not in source_ids:
                source_ids.append(relation.source_id)
        source_name = "、".join(source_names) or (source_ids[0] if source_ids else None)
        published_at = event.last_seen_at or event.first_seen_at or event.created_at
        return FeaturedEventRow(
            event_id=int(event.id),
            rank=int(snapshot.rank or 0),
            title=event.title,
            summary=event.summary_cn,
            url=_safe_url(event.canonical_url),
            display_score=float(snapshot.display_score or event.display_score or 0.0),
            topic=snapshot.topic or event.topic,
            content_class=snapshot.content_class or event.content_class,
            source_name=source_name,
            source_group=snapshot.source_group or event.source_group or (source_groups[0] if source_groups else None),
            source_subtype=source_subtype,
            source_ids=tuple(source_ids),
            risk_flags=_json_list(event.risk_flags_json),
            published_at=published_at,
        )

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
            .where(IntelItem.status != "hotspot")
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
            if filters.status.strip() == "hotspot":
                stmt = stmt.where(false())
            else:
                stmt = stmt.where(IntelItem.status == filters.status.strip())
        if filters.ai_keep is not None:
            stmt = stmt.where(AIItemReview.keep.is_(filters.ai_keep))

        items: list[AllItemRow] = []
        for item, source, review in self.session.execute(stmt).all():
            items.append(
                AllItemRow(
                    item_id=item.id,
                    title=item.title,
                    url=_safe_url(item.canonical_url),
                    source_name=source.name,
                    source_group=source.source_group,
                    source_subtype=source.source_subtype,
                    source_role=source.source_role,
                    spam_risk=source.spam_risk,
                    selection_score=item.selection_score,
                    status=item.status,
                    ai_confidence=review.confidence if review else None,
                    ai_keep=review.keep if review else None,
                    summary_cn=(review.summary_cn if review else None) or item.summary,
                    published_at=item.published_at,
                    fetched_at=item.captured_at,
                    content_class=item.content_class,
                    selection_reason=item.selection_reason,
                    ai_status=review.status if review else None,
                )
            )
        return items

    def search_content(self, query: str, *, limit_per_group: int = 8) -> SearchContentResults:
        normalized_query = query.strip()
        if not normalized_query:
            return SearchContentResults.empty()
        safe_limit = min(max(limit_per_group, 1), 20)
        like = f"%{normalized_query}%"
        selected_items = self._search_selected_items(like=like, limit=safe_limit)
        items = [
            SearchResultRow(
                result_type="item",
                id=row.item_id,
                title=row.title,
                summary=row.summary_cn,
                url=row.url,
                source_name=row.source_name,
                item_id=row.item_id,
                score=row.ai_confidence if row.ai_confidence is not None else row.selection_score,
                badges=[badge for badge in ["item", row.source_group, row.status] if badge],
                published_at=row.published_at,
                created_at=row.fetched_at,
                content_class=row.content_class,
                status=row.status,
                ai_status=row.ai_status,
                selection_reason=row.selection_reason,
            )
            for row in self.list_all_items(filters=AllItemFilters(query=normalized_query), page_size=safe_limit)
        ]
        return SearchContentResults(
            query=normalized_query,
            selected_items=selected_items,
            items=items,
        )

    def _search_selected_items(self, *, like: str, limit: int) -> list[SearchResultRow]:
        stmt = (
            select(IntelItem, Source, AIItemReview)
            .join(Source, IntelItem.source_id == Source.id)
            .join(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .where(
                IntelItem.status == "selected",
                AIItemReview.status == "success",
                AIItemReview.keep.is_(True),
            )
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
                    result_type="selected",
                    id=item.id,
                    title=item.title,
                    summary=(review.summary_cn if review else None) or item.summary,
                    url=_safe_url(item.canonical_url),
                    source_name=source.name,
                    item_id=item.id,
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


def _credibility(review: AIItemReview | None) -> int:
    return max(0, min(int(review.confidence if review else 0), 100))
