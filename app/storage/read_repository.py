"""Read-only queries over published date-addressed daily reports.

Temporary builds, raw items and AI stage rows live in per-date
filesystem audit workspaces, never in the published database.  The UI
therefore reads only final report entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai.skills.intel_triage import INTEL_TOPICS, normalize_topic
from app.storage.models import DailyEdition, DailyEditionReportEntry, Source


CONTENT_CLASSES = ("official_model_company", "project_tool", "community_social", "news_media")


@dataclass(frozen=True)
class FacetCount:
    value: str
    count: int


@dataclass(frozen=True)
class DashboardStats:
    edition_date: str | None
    status: str | None
    published_at: datetime | None
    selected_items: int
    category_counts: tuple[FacetCount, ...] = ()
    source_counts: tuple[FacetCount, ...] = ()


@dataclass(frozen=True)
class DailyEditionView:
    edition_date: str
    selected_items: int
    candidate_items: int
    status: str
    published_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class FeaturedItemRow:
    id: int
    title: str
    summary: str | None
    selection_reason: str | None
    url: str | None
    risk_note: str | None
    status: str
    display_score: int
    content_class: str | None
    source_name: str | None
    source_group: str | None
    risk_flags: list[str]
    published_at: datetime | None
    created_at: datetime | None
    screen_decision: str | None = None
    screen_confidence: int | None = None
    screen_reason_code: str | None = None
    screen_reason: str | None = None
    screen_risk_flags: list[str] = field(default_factory=list)
    ai_status: str | None = None
    topic_category: str | None = None
    source_id: str | None = None
    source_transport: str | None = None
    source_url: str | None = None
    account_url: str | None = None


@dataclass(frozen=True)
class FeaturedEventRow:
    event_id: int
    display_order: int
    title: str
    original_title: str
    summary: str | None
    url: str | None
    display_score: float
    topic: str | None
    content_class: str | None
    source_name: str | None
    source_group: str | None
    source_ids: tuple[str, ...]
    risk_flags: list[str]
    published_at: datetime | None
    keywords: tuple[str, ...] = ()
    entities: tuple[dict[str, object], ...] = ()
    provenance: str = "published"
    source_refs: tuple[dict[str, object], ...] = ()
    verification_refs: tuple[dict[str, object], ...] = ()

    @property
    def id(self) -> int:
        return self.event_id


@dataclass(frozen=True)
class EventMemberRow:
    item_id: int
    title: str
    summary: str | None
    url: str | None
    published_at: datetime | None
    captured_at: datetime | None
    source_id: str | None
    source_name: str | None
    source_group: str | None
    source_url: str | None
    is_primary: bool
    match_type: str | None
    match_confidence: int | None
    screen_decision: str | None = None
    screen_reason_code: str | None = None
    screen_reason: str | None = None
    screen_confidence: int | None = None
    screen_risk_flags: tuple[str, ...] = ()
    review_status: str | None = None
    review_topic: str | None = None
    review_summary: str | None = None
    review_reason: str | None = None
    review_score: int | None = None
    review_confidence: int | None = None
    review_risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventDetailRow:
    event: FeaturedEventRow
    selection_reason: str | None
    resolution_method: str | None
    resolution_confidence: int | None
    members: tuple[EventMemberRow, ...]


@dataclass(frozen=True)
class AllItemFilters:
    query: str | None = None
    source_group: str | None = None
    status: str | None = None
    screen_decision: str | None = None
    screen_confidence: int | None = None
    content_class: str | None = None
    topic_category: str | None = None


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
    topic_category: str | None = None
    source_group: str | None = None
    source_transport: str | None = None


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
    source_groups: tuple[str, ...]
    content_classes: tuple[str, ...]
    statuses: tuple[str, ...]
    topic_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    name: str
    transport: str
    source_group: str | None
    content_class: str | None
    url: str | None
    account_url: str | None
    health_status: str | None
    consecutive_failures: int
    item_count: int


class UIReadRepository:
    """Stable UI boundary backed exclusively by final daily reports."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_daily_editions(self) -> tuple[DailyEditionView, ...]:
        rows = self.session.execute(
            select(DailyEdition, func.count(DailyEditionReportEntry.id))
            .outerjoin(DailyEditionReportEntry, DailyEditionReportEntry.edition_id == DailyEdition.id)
            .where(DailyEdition.published_at.is_not(None))
            .group_by(DailyEdition.id)
            .order_by(DailyEdition.edition_date.desc(), DailyEdition.published_at.desc())
        ).all()
        return tuple(self._edition_view(edition, selected) for edition, selected in rows)

    def resolve_edition(self, *, edition_date: str | None = None) -> DailyEditionView | None:
        normalized = _normalize_edition_date(edition_date)
        if edition_date is not None and normalized is None:
            return None
        stmt = select(DailyEdition).where(DailyEdition.published_at.is_not(None))
        if normalized is not None:
            stmt = stmt.where(DailyEdition.edition_date == date.fromisoformat(normalized))
        else:
            stmt = stmt.order_by(DailyEdition.edition_date.desc(), DailyEdition.published_at.desc())
        edition = self.session.scalar(stmt)
        if edition is None:
            return None
        return self._edition_view(edition, self._count_entries(int(edition.id)))

    def get_edition_summary(self, edition: DailyEditionView | None) -> dict[str, Any] | None:
        if edition is None:
            return None
        return {
            "edition_date": edition.edition_date,
            "funnel": {
                "candidate": edition.candidate_items,
                "published": edition.selected_items,
                "selected": edition.selected_items,
            },
            "stages": {"publication": {"status": edition.status, "selected": edition.selected_items}},
            "failure_reasons": [],
        }

    def get_dashboard_stats(self, *, edition: DailyEditionView | None = None) -> DashboardStats:
        active = edition or self.resolve_edition()
        if active is None:
            return DashboardStats(None, None, None, 0)
        entries = self._query_entries(active.edition_date, limit=1000)
        return DashboardStats(
            edition_date=active.edition_date,
            status=active.status,
            published_at=active.published_at,
            selected_items=len(entries),
            category_counts=_facet_counts(entry.topic or entry.content_class or "未分类" for entry in entries),
            source_counts=_facet_counts(entry.source_group or "general" for entry in entries),
        )

    def list_filter_options(self) -> UIFilterOptions:
        entries = self._published_entries()
        return UIFilterOptions(
            source_groups=tuple(sorted({entry.source_group for entry in entries if entry.source_group})),
            content_classes=tuple(sorted(set(CONTENT_CLASSES).union(entry.content_class for entry in entries if entry.content_class))),
            statuses=("selected",),
            topic_categories=INTEL_TOPICS,
        )

    def list_featured_cards(
        self,
        *,
        category: str | None = None,
        source_group: str | None = None,
        content_class: str | None = None,
        query: str | None = None,
        edition: DailyEditionView | None = None,
        edition_date: str | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> list[FeaturedItemRow]:
        active = edition or self.resolve_edition(edition_date=edition_date)
        if active is None or limit <= 0:
            return []
        entries = self._query_entries(
            active.edition_date,
            category=category,
            source_group=source_group,
            content_class=content_class,
            query=query,
            offset=offset,
            limit=limit,
        )
        sources = self._sources_by_id(entries)
        return [self._card_from_entry(entry, sources) for entry in entries]

    def list_all_dynamics(
        self,
        *,
        edition: DailyEditionView | None = None,
        edition_date: str | None = None,
        query: str | None = None,
        source_group: str | None = None,
        content_class: str | None = None,
        topic_category: str | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> tuple[list[EventMemberRow], int]:
        active = edition or self.resolve_edition(edition_date=edition_date)
        if active is None or limit <= 0:
            return [], 0

        entries = self._query_entries(
            active.edition_date,
            category=topic_category,
            source_group=source_group,
            content_class=content_class,
            query=query,
            offset=0,
            limit=1000,
        )

        all_members = []
        for entry in entries:
            all_members.extend(self._members_from_entry(entry))

        seen = set()
        unique_members = []
        for member in all_members:
            if member.item_id not in seen:
                seen.add(member.item_id)
                unique_members.append(member)

        unique_members.sort(key=lambda x: (-(x.review_score or 0), x.item_id))

        total = len(unique_members)
        paginated = unique_members[offset:offset + limit]

        return paginated, total

    def list_featured_events(
        self,
        *,
        edition: DailyEditionView | None = None,
        edition_date: str | None = None,
        topic: str | None = None,
        content_class: str | None = None,
        limit: int = 30,
    ) -> list[FeaturedEventRow]:
        active = edition or self.resolve_edition(edition_date=edition_date)
        if active is None or limit <= 0:
            return []
        entries = self._query_entries(active.edition_date, topic=topic, content_class=content_class, limit=limit)
        sources = self._sources_by_id(entries)
        return [self._event_from_entry(entry, sources) for entry in entries]

    def get_selected_event_detail(
        self,
        event_id: int,
        *,
        edition: DailyEditionView | None = None,
        edition_date: str | None = None,
    ) -> EventDetailRow | None:
        active = edition or self.resolve_edition(edition_date=edition_date)
        if active is None:
            return None
        daily = self._daily_edition(active.edition_date)
        if daily is None:
            return None
        entry = self.session.scalar(
            select(DailyEditionReportEntry).where(
                DailyEditionReportEntry.edition_id == int(daily.id),
                DailyEditionReportEntry.id == int(event_id),
            )
        )
        if entry is None:
            return None
        metadata = entry.metadata_dict
        return EventDetailRow(
            event=self._event_from_entry(entry, self._sources_by_id([entry])),
            selection_reason=_text(metadata.get("reason")),
            resolution_method="published_daily_report",
            resolution_confidence=None,
            members=self._members_from_entry(entry),
        )

    def search_content(
        self,
        query: str,
        *,
        edition: DailyEditionView | None = None,
        edition_date: str | None = None,
        limit_per_group: int = 8,
    ) -> SearchContentResults:
        normalized = query.strip()
        if not normalized:
            return SearchContentResults.empty()
        active = edition or self.resolve_edition(edition_date=edition_date)
        if active is None:
            return SearchContentResults(query=normalized, selected_items=[], items=[])
        cards = self.list_featured_cards(edition=active, query=normalized, limit=min(max(limit_per_group, 1), 20))
        return SearchContentResults(
            query=normalized,
            selected_items=[
                SearchResultRow(
                    result_type="event", id=row.id, title=row.title, summary=row.summary, url=row.url,
                    source_name=row.source_name, item_id=None, score=row.display_score,
                    badges=[value for value in ("selected", row.content_class, row.source_group) if value],
                    published_at=row.published_at, created_at=row.created_at, content_class=row.content_class,
                    status="selected", ai_status="published_daily_report", selection_reason=row.selection_reason,
                    topic_category=row.topic_category, source_group=row.source_group,
                    source_transport=row.source_transport,
                )
                for row in cards
            ],
            items=[],
        )

    def list_sources(self) -> list[SourceRow]:
        counts: dict[str, int] = {}
        for entry in self._published_entries():
            for source_id in set(entry.source_ids):
                counts[source_id] = counts.get(source_id, 0) + 1
        return [
            SourceRow(
                source_id=source.id, name=source.name, transport=source.transport,
                source_group=source.source_group, content_class=source.content_class,
                url=_safe_url(source.url), account_url=_safe_url(source.account_url),
                health_status=source.health_status, consecutive_failures=int(source.consecutive_failures or 0),
                item_count=counts.get(source.id, 0),
            )
            for source in self.session.scalars(select(Source).order_by(Source.priority.asc(), Source.name.asc())).all()
        ]

    def _daily_edition(self, edition_date: str) -> DailyEdition | None:
        normalized = _normalize_edition_date(edition_date)
        if normalized is None:
            return None
        return self.session.scalar(
            select(DailyEdition).where(
                DailyEdition.edition_date == date.fromisoformat(normalized),
                DailyEdition.status == "published",
                DailyEdition.published_at.is_not(None),
            )
        )

    def _published_entries(self) -> list[DailyEditionReportEntry]:
        return list(self.session.scalars(
            select(DailyEditionReportEntry)
            .join(DailyEdition, DailyEdition.id == DailyEditionReportEntry.edition_id)
            .where(DailyEdition.status == "published", DailyEdition.published_at.is_not(None))
            .order_by(DailyEdition.edition_date.desc(), DailyEditionReportEntry.display_order.asc())
        ).all())

    def _query_entries(
        self,
        edition_date: str,
        *,
        topic: str | None = None,
        category: str | None = None,
        source_group: str | None = None,
        content_class: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[DailyEditionReportEntry]:
        daily = self._daily_edition(edition_date)
        if daily is None:
            return []
        stmt = select(DailyEditionReportEntry).where(DailyEditionReportEntry.edition_id == int(daily.id))
        if topic:
            stmt = stmt.where(DailyEditionReportEntry.topic == (normalize_topic(topic) or topic))
        if category:
            normalized_category = normalize_topic(category) or category
            stmt = stmt.where(or_(DailyEditionReportEntry.topic == normalized_category, DailyEditionReportEntry.content_class == category))
        if source_group:
            stmt = stmt.where(DailyEditionReportEntry.source_group == source_group)
        if content_class:
            stmt = stmt.where(DailyEditionReportEntry.content_class == content_class)
        normalized_query = query.strip() if query else ""
        if normalized_query:
            like = f"%{normalized_query}%"
            stmt = stmt.where(or_(
                DailyEditionReportEntry.title.ilike(like),
                DailyEditionReportEntry.original_title.ilike(like),
                DailyEditionReportEntry.summary.ilike(like),
                DailyEditionReportEntry.url.ilike(like),
            ))
        return list(self.session.scalars(
            stmt.order_by(DailyEditionReportEntry.display_order.asc(), DailyEditionReportEntry.id.asc())
            .offset(max(0, offset)).limit(min(max(limit, 1), 1000))
        ).all())

    def _count_entries(self, edition_id: int) -> int:
        return int(self.session.execute(
            select(func.count()).select_from(DailyEditionReportEntry).where(DailyEditionReportEntry.edition_id == int(edition_id))
        ).scalar_one())

    @staticmethod
    def _edition_view(edition: DailyEdition, selected: int) -> DailyEditionView:
        return DailyEditionView(
            edition_date=edition.edition_date.isoformat(), selected_items=int(selected),
            candidate_items=int(edition.candidate_count or 0), status="published",
            published_at=edition.published_at, updated_at=edition.updated_at,
        )

    def _sources_by_id(self, entries: Iterable[DailyEditionReportEntry]) -> dict[str, Source]:
        source_ids = sorted({source_id for entry in entries for source_id in entry.source_ids if source_id})
        if not source_ids:
            return {}
        return {source.id: source for source in self.session.scalars(select(Source).where(Source.id.in_(source_ids))).all()}

    def _event_from_entry(self, entry: DailyEditionReportEntry, sources: dict[str, Source]) -> FeaturedEventRow:
        metadata = entry.metadata_dict
        refs = tuple(_public_source_ref(value) for value in entry.source_refs)
        verification_refs = tuple(_public_verification_ref(value) for value in entry.verification_refs)
        primary = _primary_ref(refs)
        source = sources.get(_text(primary.get("source_id")) if primary else "")
        provenance = metadata.get("provenance")
        if isinstance(provenance, dict):
            provenance = provenance.get("kind")
        return FeaturedEventRow(
            event_id=int(entry.id), display_order=int(entry.display_order), title=entry.title,
            original_title=entry.original_title or entry.title, summary=entry.summary, url=_safe_url(entry.url),
            display_score=float(entry.display_score or 0.0), topic=entry.topic, content_class=entry.content_class,
            source_name=_text(primary.get("source_name")) if primary else None or (source.name if source is not None else (entry.source_ids[0] if entry.source_ids else None)),
            source_group=(_text(primary.get("source_group")) if primary else None) or entry.source_group,
            source_ids=tuple(entry.source_ids), risk_flags=list(entry.risk_flags), published_at=entry.published_at,
            keywords=tuple(entry.keywords), entities=tuple(value for value in entry.entities if isinstance(value, dict)),
            provenance=_text(provenance) or "published", source_refs=refs,
            verification_refs=verification_refs,
        )

    def _card_from_entry(self, entry: DailyEditionReportEntry, sources: dict[str, Source]) -> FeaturedItemRow:
        event = self._event_from_entry(entry, sources)
        primary = _primary_ref(event.source_refs)
        source = sources.get(_text(primary.get("source_id")) if primary else "")
        metadata = entry.metadata_dict
        return FeaturedItemRow(
            id=event.event_id, title=event.title, summary=event.summary,
            selection_reason=_text(metadata.get("reason")), url=event.url,
            risk_note="；".join(event.risk_flags) if event.risk_flags else None, status="selected",
            display_score=int(round(event.display_score)),
            content_class=event.content_class, source_name=event.source_name, source_group=event.source_group,
            risk_flags=event.risk_flags, published_at=event.published_at,
            created_at=entry.created_at, ai_status="published_daily_report", topic_category=event.topic,
            source_id=_text(primary.get("source_id")) if primary else (event.source_ids[0] if event.source_ids else None),
            source_transport=source.transport if source is not None else None,
            source_url=_safe_url(_text(primary.get("source_url")) if primary else None) or (_safe_url(source.url) if source is not None else None),
            account_url=_safe_url(source.account_url) if source is not None else None,
        )

    @staticmethod
    def _members_from_entry(entry: DailyEditionReportEntry) -> tuple[EventMemberRow, ...]:
        rows: list[EventMemberRow] = []
        for position, ref in enumerate(entry.source_refs, start=1):
            rows.append(EventMemberRow(
                item_id=_int(ref.get("item_id")) or -(int(entry.id) * 1000 + position),
                title=_text(ref.get("title")) or entry.original_title or entry.title,
                summary=entry.summary, url=_safe_url(_text(ref.get("source_url"))) or _safe_url(entry.url),
                published_at=entry.published_at, captured_at=None, source_id=_text(ref.get("source_id")),
                source_name=_text(ref.get("source_name")), source_group=_text(ref.get("source_group")) or entry.source_group,
                source_url=_safe_url(_text(ref.get("source_url"))), is_primary=bool(ref.get("is_primary")),
                match_type=_text(ref.get("match_type")), match_confidence=_int(ref.get("match_confidence")),
                review_topic=entry.topic, review_summary=entry.summary,
            ))
        return tuple(sorted(rows, key=lambda row: (not row.is_primary, row.item_id)))


def _facet_counts(values: Iterable[str]) -> tuple[FacetCount, ...]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return tuple(FacetCount(value=value, count=count) for value, count in sorted(counts.items(), key=lambda row: (-row[1], row[0])))


def _primary_ref(refs: Sequence[dict[str, object]]) -> dict[str, object] | None:
    return next((ref for ref in refs if bool(ref.get("is_primary"))), refs[0] if refs else None)


def _public_source_ref(value: dict[str, object]) -> dict[str, object]:
    ref = dict(value)
    ref["source_url"] = _safe_url(_text(ref.get("source_url")))
    return ref


def _public_verification_ref(value: dict[str, object]) -> dict[str, object]:
    ref = dict(value)
    ref["url"] = _safe_url(_text(ref.get("url")))
    ref["final_url"] = _safe_url(_text(ref.get("final_url")))
    return ref


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int(value: object, *, default: int | None = None) -> int | None:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_edition_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else None
