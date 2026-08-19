"""Read-only DTO queries for the AI-only web UI.

The repository is the only database boundary used by web routes. It reads
unified items, their structured AI review, and source attribution.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Sequence
from urllib.parse import urlsplit

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app.config.settings import DEFAULT_AI_REVIEW_CATEGORIES
from app.domain.categories import fallback_topic_category
from app.storage.run_snapshot_summary import build_run_snapshot_summary
from app.storage.models import (
    AIItemReview,
    AIItemScreen,
    DailyEdition,
    DailyEditionReportEntry,
    IntelEvent,
    IntelEventItem,
    IntelEventStageDSnapshot,
    IntelItem,
    IntelRun,
    IntelRunItem,
    IntelRunStage,
    Source,
)


FINAL_ITEM_STATUSES = ("candidate",)
PENDING_ITEM_STATUSES = ("new", "screen_failed", "analysis_failed")
DASHBOARD_PENDING_ITEM_STATUSES = ("new", "screen_failed", "analysis_failed")
ITEM_STATUSES = FINAL_ITEM_STATUSES + PENDING_ITEM_STATUSES + ("screened_out", "analysis_filtered")
CONTENT_CLASSES = ("official_model_company", "project_tool", "community_social", "news_media")
_HIDDEN_EVENT_MEMBER_STATUSES = frozenset(
    {
        "screened_out",
        "analysis_filtered",
        "screen_failed",
        "analysis_failed",
        "time_too_old",
        "time_future_timestamp",
        "time_missing_published_at",
    }
)


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
    active_run_id: int | None = None
    active_edition_date: str | None = None
    active_snapshot_key: str | None = None
    active_partial: bool = False
    active_partial_reason: str | None = None
    category_counts: tuple["FacetCount", ...] = ()
    source_counts: tuple["FacetCount", ...] = ()


@dataclass(frozen=True)
class FacetCount:
    value: str
    count: int


@dataclass(frozen=True)
class RunSnapshot:
    """One user-selectable Stage-D snapshot grouped under a daily edition.

    ``run_id`` remains the internal immutable foreign-key value.  The
    ``edition_date`` is the human-facing daily identifier; multiple runs may
    share one date and the UI resolves that date to the newest Stage-D snapshot.
    """

    run_id: int | None
    edition_date: str | None
    snapshot_key: str
    selected_items: int
    stage_d_items: int
    run_type: str | None
    run_status: str | None
    partial: bool
    partial_reason: str | None
    started_at: datetime | None
    updated_at: datetime | None
    edition_timezone: str | None = None


@dataclass(frozen=True)
class RunSummary:
    """Public metadata for a durable processing run, without raw task data."""

    run_id: int | None
    edition_date: str | None
    edition_timezone: str | None
    run_type: str
    status: str
    partial: bool
    partial_reason: str | None
    reference_time: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    scope_frozen: bool
    snapshots: tuple[RunSnapshot, ...] = ()


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
    screen_decision: str | None = None
    screen_confidence: int | None = None
    screen_reason_code: str | None = None
    screen_reason: str | None = None
    screen_risk_flags: list[str] = field(default_factory=list)
    ai_status: str | None = None
    topic_category: str | None = None
    source_id: str | None = None
    source_transport: str | None = None
    source_tier: str | None = None
    source_role: str | None = None
    source_url: str | None = None
    account_url: str | None = None
    presentation_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeaturedEventRow:
    """Event-level card backed by the selected editorial snapshot."""

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
    source_subtype: str | None
    source_ids: tuple[str, ...]
    risk_flags: list[str]
    published_at: datetime | None
    keywords: tuple[str, ...] = ()
    entities: tuple[dict[str, object], ...] = ()
    provenance: str = "new"
    source_refs: tuple[dict[str, object], ...] = ()
    story_family_id: str | None = None
    family_position: int | None = None
    presentation_labels: tuple[str, ...] = ()

    @property
    def id(self) -> int:
        """Compatibility alias for templates/card consumers."""

        return self.event_id


@dataclass(frozen=True)
class EventMemberRow:
    """Safe, public provenance for one member of a selected event."""

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
    screen_decision: str | None
    screen_reason_code: str | None
    screen_reason: str | None
    screen_confidence: int | None
    screen_risk_flags: tuple[str, ...]
    review_status: str | None
    review_topic: str | None
    review_summary: str | None
    review_reason: str | None
    review_score: int | None
    review_confidence: int | None
    review_risk_flags: tuple[str, ...]


@dataclass(frozen=True)
class EventDetailRow:
    run_id: int | None
    snapshot_key: str
    event: FeaturedEventRow
    selection_reason: str | None
    resolution_method: str | None
    resolution_confidence: int | None
    members: tuple[EventMemberRow, ...]


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
    screen_decision: str | None
    screen_confidence: int | None
    summary_cn: str | None
    published_at: datetime | None
    fetched_at: datetime | None
    content_class: str | None = None
    selection_reason: str | None = None
    ai_status: str | None = None
    screen_reason_code: str | None = None
    screen_reason: str | None = None
    screen_risk_flags: list[str] = field(default_factory=list)
    topic_category: str | None = None
    source_id: str | None = None
    source_transport: str | None = None
    source_tier: str | None = None
    source_url: str | None = None
    account_url: str | None = None

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
    source_tier: str | None = None


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
    topic_categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    name: str
    transport: str
    source_group: str | None
    source_subtype: str | None
    source_role: str | None
    tier: str | None
    content_class: str | None
    url: str | None
    account_url: str | None
    health_status: str | None
    consecutive_failures: int
    item_count: int


class UIReadRepository:
    """Stable read boundary backed only by the v2 tables."""

    def __init__(self, session: Session, topic_categories: Sequence[str] | None = None) -> None:
        self.session = session
        configured = tuple(str(value).strip() for value in (topic_categories or DEFAULT_AI_REVIEW_CATEGORIES) if str(value).strip())
        self.topic_categories = configured or DEFAULT_AI_REVIEW_CATEGORIES

    def _published_edition_snapshots(self) -> list[RunSnapshot]:
        """Return the date-level reports that survive build cleanup."""

        rows = self.session.execute(
            select(DailyEdition, func.count(DailyEditionReportEntry.id))
            .outerjoin(
                DailyEditionReportEntry,
                DailyEditionReportEntry.edition_id == DailyEdition.id,
            )
            .where(DailyEdition.published_at.is_not(None))
            .group_by(DailyEdition.id)
            .order_by(DailyEdition.edition_date.desc(), DailyEdition.published_at.desc())
        ).all()
        return [
            RunSnapshot(
                run_id=None,
                edition_date=edition.edition_date.isoformat(),
                # This is an in-process compatibility token only. It is not
                # emitted by the public API and callers should address the
                # edition by date.
                snapshot_key=f"daily-{edition.edition_date.isoformat()}",
                selected_items=int(selected),
                stage_d_items=int(selected),
                run_type="daily_edition",
                run_status="published",
                partial=False,
                partial_reason=None,
                started_at=edition.published_at,
                updated_at=edition.updated_at,
                edition_timezone="Asia/Shanghai",
            )
            for edition, selected in rows
        ]

    def _daily_edition_for_snapshot(self, snapshot: RunSnapshot) -> DailyEdition | None:
        if snapshot.run_id is not None or not snapshot.edition_date:
            return None
        normalized = _normalize_edition_date(snapshot.edition_date)
        if normalized is None:
            return None
        return self.session.scalar(
            select(DailyEdition).where(
                DailyEdition.edition_date == date.fromisoformat(normalized),
                DailyEdition.published_at.is_not(None),
            )
        )

    def list_run_snapshots(self) -> list[RunSnapshot]:
        """List Stage-D snapshots without treating ``latest`` as a magic key."""

        published = self._published_edition_snapshots()
        published_dates = {value.edition_date for value in published if value.edition_date}
        stage_d_keys = {
            int(stage.run_id): str(value)
            for stage in self.session.scalars(
                select(IntelRunStage).where(IntelRunStage.stage_name == "stage_d")
            ).all()
            if (value := stage.metadata_dict.get("snapshot_key"))
        }
        groups: dict[tuple[int | None, str], dict[str, Any]] = {}
        rows = self.session.execute(
            select(IntelEventStageDSnapshot, IntelRun)
            .outerjoin(IntelRun, IntelRun.id == IntelEventStageDSnapshot.run_id)
            .order_by(
                IntelEventStageDSnapshot.updated_at.desc(),
                IntelEventStageDSnapshot.id.desc(),
            )
        ).all()
        for snapshot, run in rows:
            key = (int(snapshot.run_id) if snapshot.run_id is not None else None, str(snapshot.snapshot_key))
            group = groups.get(key)
            if group is None:
                group = {
                    "run": run,
                    "snapshot_key": str(snapshot.snapshot_key),
                    "selected": 0,
                    "stage_d": 0,
                    "updated_at": snapshot.updated_at,
                }
                groups[key] = group
            group["stage_d"] += 1
            if snapshot.selected:
                group["selected"] += 1
            if _sort_timestamp(snapshot.updated_at) > _sort_timestamp(group["updated_at"]):
                group["updated_at"] = snapshot.updated_at

        result: list[RunSnapshot] = []
        for (run_id, snapshot_key), group in groups.items():
            if not group["selected"]:
                continue
            run = group["run"]
            result.append(
                RunSnapshot(
                    run_id=run_id,
                    edition_date=run.edition_date if run is not None else None,
                    snapshot_key=snapshot_key,
                    selected_items=int(group["selected"]),
                    stage_d_items=int(group["stage_d"]),
                    run_type=run.run_type if run is not None else None,
                    run_status=("partial" if run is not None and run.partial else run.status if run is not None else None),
                    partial=bool(run.partial) if run is not None else False,
                    partial_reason=run.partial_reason if run is not None else None,
                    started_at=run.started_at if run is not None else None,
                    updated_at=group["updated_at"],
                    edition_timezone=(
                        str(run.scope.get("edition_timezone") or "") or None
                        if run is not None
                        else None
                    ),
                )
            )
        # A date-level published report is authoritative. Legacy snapshots
        # are retained only as a read compatibility fallback for databases
        # that have not yet been migrated.
        result = [
            *published,
            *(value for value in result if value.edition_date not in published_dates),
        ]
        return sorted(
            result,
            key=lambda value: (
                _sort_timestamp(value.started_at),
                int(value.run_id or 0),
                int(stage_d_keys.get(value.run_id) == value.snapshot_key),
                _sort_timestamp(value.updated_at),
            ),
            reverse=True,
        )

    def list_runs(self) -> list[RunSummary]:
        """List public daily reports, with legacy builds as a fallback."""

        published_snapshots = self._published_edition_snapshots()
        published_dates = {value.edition_date for value in published_snapshots if value.edition_date}
        snapshots_by_run: dict[int, list[RunSnapshot]] = {}
        for snapshot in self.list_run_snapshots():
            if snapshot.run_id is not None:
                snapshots_by_run.setdefault(int(snapshot.run_id), []).append(snapshot)
        runs = self.session.scalars(
            select(IntelRun).order_by(IntelRun.started_at.desc(), IntelRun.id.desc())
        ).all()
        reports = [
            RunSummary(
                run_id=None,
                edition_date=snapshot.edition_date,
                edition_timezone=snapshot.edition_timezone,
                run_type="daily_edition",
                status="published",
                partial=False,
                partial_reason=None,
                reference_time=snapshot.started_at,
                started_at=snapshot.started_at,
                finished_at=snapshot.updated_at,
                scope_frozen=True,
                snapshots=(snapshot,),
            )
            for snapshot in published_snapshots
        ]
        legacy = [
            RunSummary(
                run_id=int(run.id),
                edition_date=run.edition_date,
                edition_timezone=str(run.scope.get("edition_timezone") or "") or None,
                run_type=run.run_type,
                status="partial" if run.partial else run.status,
                partial=bool(run.partial),
                partial_reason=run.partial_reason,
                reference_time=run.reference_time,
                started_at=run.started_at,
                finished_at=run.finished_at,
                scope_frozen=run.scope_frozen,
                snapshots=tuple(snapshots_by_run.get(int(run.id), [])),
            )
            for run in runs
            if run.edition_date not in published_dates
        ]
        return [*reports, *legacy]

    def get_run_snapshot_summary(self, snapshot: RunSnapshot | None) -> dict[str, Any] | None:
        """Return the public funnel/stage projection for one resolved snapshot."""

        if snapshot is None:
            return None
        edition = self._daily_edition_for_snapshot(snapshot)
        if edition is not None:
            selected = self._count(
                DailyEditionReportEntry,
                DailyEditionReportEntry.edition_id == int(edition.id),
            )
            return {
                "edition_date": edition.edition_date.isoformat(),
                "funnel": {
                    "raw": 0,
                    "screened": 0,
                    "analyzed": 0,
                    "clustered": selected,
                    "selected": selected,
                },
                "stages": {
                    "publication": {
                        "status": "published",
                        "selected": selected,
                    }
                },
                "failure_reasons": [],
            }
        if snapshot.run_id is None:
            return None
        run = self.session.get(IntelRun, int(snapshot.run_id))
        if run is None:
            return None
        return build_run_snapshot_summary(
            self.session,
            run=run,
            snapshot_key=snapshot.snapshot_key,
        )

    def list_daily_editions(self) -> tuple[RunSnapshot, ...]:
        """Return the current snapshot for each date, newest date first."""

        editions: dict[str, RunSnapshot] = {}
        for snapshot in self.list_run_snapshots():
            if snapshot.edition_date and snapshot.edition_date not in editions:
                editions[snapshot.edition_date] = snapshot
        return tuple(editions.values())

    def resolve_snapshot(
        self,
        *,
        edition_date: str | None = None,
        run_id: int | None = None,
        snapshot_key: str | None = None,
    ) -> RunSnapshot | None:
        """Resolve an exact or daily-current snapshot for public UI queries."""

        normalized_date = _normalize_edition_date(edition_date)
        if edition_date and normalized_date is None:
            return None
        candidates = self.list_run_snapshots()
        if run_id is not None:
            candidates = [candidate for candidate in candidates if candidate.run_id == int(run_id)]
        if normalized_date is not None:
            candidates = [candidate for candidate in candidates if candidate.edition_date == normalized_date]
        if snapshot_key:
            candidates = [candidate for candidate in candidates if candidate.snapshot_key == snapshot_key]
        return candidates[0] if candidates else None

    def get_dashboard_stats(self, *, snapshot: RunSnapshot | None = None) -> DashboardStats:
        active_snapshot = snapshot or self.resolve_snapshot()
        edition = self._daily_edition_for_snapshot(active_snapshot) if active_snapshot is not None else None
        if edition is not None:
            entries = list(
                self.session.scalars(
                    select(DailyEditionReportEntry)
                    .where(DailyEditionReportEntry.edition_id == int(edition.id))
                    .order_by(DailyEditionReportEntry.display_order.asc())
                ).all()
            )
            categories: dict[str, int] = {}
            sources: dict[str, int] = {}
            for entry in entries:
                category = str(entry.topic or entry.content_class or "未分类")
                categories[category] = categories.get(category, 0) + 1
                source_group = str(entry.source_group or "general")
                sources[source_group] = sources.get(source_group, 0) + 1
            return DashboardStats(
                raw_items=0,
                selected_items=len(entries),
                pending_items=0,
                ai_failed_items=0,
                filtered_items=0,
                rejected_items=0,
                last_run_type="daily_edition",
                last_run_status="published",
                last_run_started_at=edition.published_at,
                active_run_id=None,
                active_edition_date=edition.edition_date.isoformat(),
                active_snapshot_key=None,
                active_partial=False,
                active_partial_reason=None,
                category_counts=tuple(
                    FacetCount(value=value, count=count)
                    for value, count in sorted(categories.items(), key=lambda item: (-item[1], item[0]))
                ),
                source_counts=tuple(
                    FacetCount(value=value, count=count)
                    for value, count in sorted(sources.items(), key=lambda item: (-item[1], item[0]))
                ),
            )
        last_run = (
            self.session.get(IntelRun, int(active_snapshot.run_id))
            if active_snapshot is not None and active_snapshot.run_id is not None
            else self.session.scalars(
                select(IntelRun).order_by(IntelRun.started_at.desc(), IntelRun.id.desc()).limit(1)
            ).first()
        )
        if last_run is not None:
            status_counts = {
                str(status): int(count)
                for status, count in self.session.execute(
                    select(IntelRunItem.status, func.count())
                    .where(IntelRunItem.run_id == last_run.id)
                    .group_by(IntelRunItem.status)
                ).all()
            }
            raw_items = self._count(IntelRunItem, IntelRunItem.run_id == last_run.id)
        else:
            status_counts = {
                str(status): int(count)
                for status, count in self.session.execute(
                    select(IntelItem.status, func.count()).group_by(IntelItem.status)
                ).all()
            }
            raw_items = self._count(IntelItem)
        snapshot_conditions = self._snapshot_conditions(active_snapshot)
        selected_items = int(
            self.session.execute(
                select(func.count())
                .select_from(IntelEventStageDSnapshot)
                .where(*snapshot_conditions)
            ).scalar_one()
        )
        pending_items = sum(status_counts.get(status, 0) for status in DASHBOARD_PENDING_ITEM_STATUSES)
        run_type = "run-once" if last_run else None
        if last_run and last_run.filters_json:
            try:
                filters = json.loads(last_run.filters_json)
            except (TypeError, json.JSONDecodeError):
                filters = {}
            if isinstance(filters, dict):
                run_type = str(filters.get("stage") or filters.get("command") or run_type)
        category_counts_map: dict[str, int] = {}
        selected_rows = self.session.execute(
            select(IntelEventStageDSnapshot, IntelEvent)
            .join(IntelEvent, IntelEvent.id == IntelEventStageDSnapshot.event_id)
            .where(*snapshot_conditions)
        ).all()
        for snapshot, event in selected_rows:
            category = str(snapshot.topic or event.topic or "未分类")
            category_counts_map[category] = category_counts_map.get(category, 0) + 1
        source_counts = tuple(
            FacetCount(value=str(value or "general"), count=int(count))
            for value, count in self.session.execute(
                select(Source.source_group, func.count(IntelEventItem.id))
                .join(IntelEventItem, IntelEventItem.source_id == Source.id)
                .join(IntelEvent, IntelEvent.id == IntelEventItem.event_id)
                .join(IntelEventStageDSnapshot, IntelEventStageDSnapshot.event_id == IntelEvent.id)
                .where(*snapshot_conditions)
                .group_by(Source.source_group)
                .order_by(func.count(IntelEventItem.id).desc())
            ).all()
        )
        category_counts = tuple(
            FacetCount(value=value, count=count)
            for value, count in sorted(category_counts_map.items(), key=lambda entry: (-entry[1], entry[0]))
        )
        return DashboardStats(
            raw_items=raw_items,
            selected_items=selected_items,
            pending_items=pending_items,
            ai_failed_items=status_counts.get("screen_failed", 0) + status_counts.get("analysis_failed", 0),
            filtered_items=status_counts.get("analysis_filtered", 0),
            rejected_items=status_counts.get("screened_out", 0),
            last_run_type=run_type,
            last_run_status=("partial" if last_run and last_run.partial else last_run.status if last_run else None),
            last_run_started_at=last_run.started_at if last_run else None,
            active_run_id=active_snapshot.run_id if active_snapshot else None,
            active_edition_date=active_snapshot.edition_date if active_snapshot else None,
            active_snapshot_key=active_snapshot.snapshot_key if active_snapshot else None,
            active_partial=active_snapshot.partial if active_snapshot else False,
            active_partial_reason=active_snapshot.partial_reason if active_snapshot else None,
            category_counts=category_counts,
            source_counts=source_counts,
        )

    def list_filter_options(self) -> UIFilterOptions:
        """Return distinct filter values from persisted v2 rows.

        Source registry values are intentionally not hardcoded in the route;
        this leaves the UI correct when a deployment adds a source group or
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
        report_source_groups = tuple(
            value
            for (value,) in self.session.execute(
                select(DailyEditionReportEntry.source_group)
                .join(DailyEdition, DailyEdition.id == DailyEditionReportEntry.edition_id)
                .where(
                    DailyEdition.published_at.is_not(None),
                    DailyEditionReportEntry.source_group.is_not(None),
                    DailyEditionReportEntry.source_group != "",
                )
                .distinct()
                .order_by(DailyEditionReportEntry.source_group.asc())
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
        report_content_classes = tuple(
            value
            for (value,) in self.session.execute(
                select(DailyEditionReportEntry.content_class)
                .join(DailyEdition, DailyEdition.id == DailyEditionReportEntry.edition_id)
                .where(
                    DailyEdition.published_at.is_not(None),
                    DailyEditionReportEntry.content_class.is_not(None),
                    DailyEditionReportEntry.content_class != "",
                )
                .distinct()
            ).all()
            if value
        )
        content_classes = tuple(sorted(set(CONTENT_CLASSES).union(persisted_content_classes, report_content_classes)))
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
        statuses = tuple(sorted(set(ITEM_STATUSES).union(set(persisted_statuses))))
        persisted_topic_categories = tuple(
            value
            for (value,) in self.session.execute(
                select(AIItemReview.topic)
                .where(AIItemReview.topic.is_not(None), AIItemReview.topic != "")
                .distinct()
                .order_by(AIItemReview.topic.asc())
            ).all()
            if value
        )
        report_topic_categories = tuple(
            value
            for (value,) in self.session.execute(
                select(DailyEditionReportEntry.topic)
                .join(DailyEdition, DailyEdition.id == DailyEditionReportEntry.edition_id)
                .where(
                    DailyEdition.published_at.is_not(None),
                    DailyEditionReportEntry.topic.is_not(None),
                    DailyEditionReportEntry.topic != "",
                )
                .distinct()
            ).all()
            if value
        )
        topic_categories = tuple(dict.fromkeys((*self.topic_categories, *persisted_topic_categories, *report_topic_categories)))
        return UIFilterOptions(
            source_groups=tuple(dict.fromkeys((*source_groups, *report_source_groups))),
            content_classes=content_classes,
            statuses=statuses,
            topic_categories=topic_categories,
        )

    def list_featured_cards(
        self,
        *,
        category: str | None = None,
        source_group: str | None = None,
        content_class: str | None = None,
        query: str | None = None,
        snapshot: RunSnapshot | None = None,
        edition_date: str | None = None,
        run_id: int | None = None,
        snapshot_key: str | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> list[FeaturedItemRow]:
        if limit <= 0:
            return []
        active_snapshot = snapshot or self.resolve_snapshot(
            edition_date=edition_date,
            run_id=run_id,
            snapshot_key=snapshot_key,
        )
        if active_snapshot is None:
            return []
        return self._list_featured_event_cards(
            snapshot=active_snapshot,
            category=category,
            source_group=source_group,
            content_class=content_class,
            query=query,
            offset=offset,
            limit=min(limit, 100),
        )

    def list_featured_events(
        self,
        *,
        snapshot: RunSnapshot | None = None,
        snapshot_key: str | None = None,
        run_id: int | None = None,
        edition_date: str | None = None,
        topic: str | None = None,
        content_class: str | None = None,
        limit: int = 30,
    ) -> list[FeaturedEventRow]:
        """Read selected event cards from one immutable Stage-D snapshot."""

        if limit <= 0:
            return []
        active_snapshot = snapshot or self.resolve_snapshot(
            edition_date=edition_date,
            run_id=run_id,
            snapshot_key=snapshot_key,
        )
        if active_snapshot is None:
            return []
        if self._daily_edition_for_snapshot(active_snapshot) is not None:
            return self._list_daily_report_events(
                snapshot=active_snapshot,
                topic=topic,
                content_class=content_class,
                limit=limit,
            )
        stmt = (
            select(IntelEventStageDSnapshot, IntelEvent)
            .join(IntelEvent, IntelEvent.id == IntelEventStageDSnapshot.event_id)
            .where(*self._snapshot_conditions(active_snapshot))
            .order_by(IntelEventStageDSnapshot.display_order.asc(), IntelEvent.id.asc())
            .limit(min(limit, 100))
        )
        if topic:
            stmt = stmt.where(IntelEventStageDSnapshot.topic == topic)
        if content_class:
            stmt = stmt.where(IntelEventStageDSnapshot.content_class == content_class)
        rows: list[FeaturedEventRow] = []
        for snapshot, event in self.session.execute(stmt).all():
            rows.append(self._event_row(snapshot, event))
        return rows

    def get_selected_event_detail(
        self,
        event_id: int,
        *,
        snapshot: RunSnapshot | None = None,
        edition_date: str | None = None,
        run_id: int | None = None,
        snapshot_key: str | None = None,
    ) -> EventDetailRow | None:
        """Read one selected event and its safe, public traceability fields."""

        active_snapshot = snapshot or self.resolve_snapshot(
            edition_date=edition_date,
            run_id=run_id,
            snapshot_key=snapshot_key,
        )
        if active_snapshot is None:
            return None
        edition = self._daily_edition_for_snapshot(active_snapshot)
        if edition is not None:
            entry = self.session.scalar(
                select(DailyEditionReportEntry).where(
                    DailyEditionReportEntry.edition_id == int(edition.id),
                    DailyEditionReportEntry.id == int(event_id),
                )
            )
            if entry is None:
                return None
            metadata = entry.metadata_dict
            return EventDetailRow(
                run_id=None,
                snapshot_key=active_snapshot.snapshot_key,
                event=self._report_event_row(entry),
                selection_reason=str(metadata.get("reason") or "").strip() or None,
                resolution_method="published_report",
                resolution_confidence=100,
                members=self._report_member_rows(entry),
            )
        row = self.session.execute(
            select(IntelEventStageDSnapshot, IntelEvent)
            .join(IntelEvent, IntelEvent.id == IntelEventStageDSnapshot.event_id)
            .where(
                *self._snapshot_conditions(active_snapshot),
                IntelEvent.id == int(event_id),
            )
        ).first()
        if row is None:
            return None
        stage_d_snapshot, event = row
        members: list[EventMemberRow] = []
        for relation in event.event_items:
            item = relation.item
            if item is None or item.status in _HIDDEN_EVENT_MEMBER_STATUSES:
                continue
            source = relation.source or item.source
            screen = item.ai_screen
            review = item.ai_review
            members.append(
                EventMemberRow(
                    item_id=int(item.id),
                    title=relation.source_title or item.title,
                    summary=(review.summary_cn if review is not None else None) or item.summary,
                    url=_safe_url(relation.source_url or item.canonical_url),
                    published_at=item.published_at,
                    captured_at=item.captured_at,
                    source_id=relation.source_id or item.source_id,
                    source_name=source.name if source is not None else None,
                    source_group=relation.source_group or (source.source_group if source is not None else None),
                    source_url=_safe_url(source.url) if source is not None else None,
                    is_primary=bool(relation.is_primary),
                    match_type=relation.match_type,
                    match_confidence=relation.match_confidence,
                    screen_decision=screen.decision if screen is not None else None,
                    screen_reason_code=screen.reason_code if screen is not None else None,
                    screen_reason=screen.reason if screen is not None else None,
                    screen_confidence=screen.confidence if screen is not None else None,
                    screen_risk_flags=tuple(screen.risk_flags if screen is not None else []),
                    review_status=review.status if review is not None else None,
                    review_topic=review.topic if review is not None else None,
                    review_summary=review.summary_cn if review is not None else None,
                    review_reason=review.reason if review is not None else None,
                    review_score=review.selection_score if review is not None else None,
                    review_confidence=review.confidence if review is not None else None,
                    review_risk_flags=tuple(review.risk_flags if review is not None else []),
                )
            )
        members.sort(
            key=lambda member: (
                not member.is_primary,
                -_sort_timestamp(member.published_at),
                member.item_id,
            )
        )
        return EventDetailRow(
            run_id=active_snapshot.run_id,
            snapshot_key=active_snapshot.snapshot_key,
            event=self._event_row(stage_d_snapshot, event),
            selection_reason=stage_d_snapshot.reason,
            resolution_method=event.resolution_method,
            resolution_confidence=event.resolution_confidence,
            members=tuple(members),
        )

    # Descriptive aliases used by route/integration callers.
    list_homepage_events = list_featured_events
    list_stage_d_snapshot = list_featured_events

    def _list_featured_event_cards(
        self,
        *,
        snapshot: RunSnapshot,
        category: str | None,
        source_group: str | None,
        content_class: str | None,
        query: str | None,
        offset: int,
        limit: int,
    ) -> list[FeaturedItemRow]:
        if self._daily_edition_for_snapshot(snapshot) is not None:
            return self._list_daily_report_cards(
                snapshot=snapshot,
                category=category,
                source_group=source_group,
                content_class=content_class,
                query=query,
                offset=offset,
                limit=limit,
            )
        stmt = (
            select(IntelEventStageDSnapshot, IntelEvent)
            .join(IntelEvent, IntelEvent.id == IntelEventStageDSnapshot.event_id)
            .where(*self._snapshot_conditions(snapshot))
            .order_by(IntelEventStageDSnapshot.display_order.asc(), IntelEvent.id.asc())
            .offset(max(0, offset))
            .limit(min(limit, 100))
        )
        if category:
            stmt = stmt.where(
                or_(
                    IntelEventStageDSnapshot.topic == category,
                    IntelEventStageDSnapshot.content_class == category,
                )
            )
        if source_group:
            stmt = stmt.where(IntelEventStageDSnapshot.source_group == source_group)
        if content_class:
            stmt = stmt.where(IntelEventStageDSnapshot.content_class == content_class)
        normalized_query = query.strip() if query else ""
        if normalized_query:
            like = f"%{normalized_query}%"
            stmt = stmt.where(
                or_(
                    IntelEvent.title.ilike(like),
                    IntelEvent.summary_cn.ilike(like),
                    IntelEvent.canonical_url.ilike(like),
                )
            )
        cards: list[FeaturedItemRow] = []
        for snapshot, event in self.session.execute(stmt).all():
            row = self._event_row(snapshot, event)
            cards.append(
                FeaturedItemRow(
                    id=row.event_id,
                    title=row.title,
                    summary=row.summary,
                    selection_reason=f"stage_d:{row.display_order}",
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
                    screen_decision="pass",
                    screen_confidence=100,
                    ai_status="editorial_snapshot",
                    topic_category=row.topic,
                    source_id=row.source_ids[0] if row.source_ids else None,
                    presentation_labels=row.presentation_labels,
                )
            )
        return cards

    def _daily_report_entries(
        self,
        *,
        snapshot: RunSnapshot,
        topic: str | None = None,
        category: str | None = None,
        source_group: str | None = None,
        content_class: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[DailyEditionReportEntry]:
        edition = self._daily_edition_for_snapshot(snapshot)
        if edition is None:
            return []
        stmt = (
            select(DailyEditionReportEntry)
            .where(DailyEditionReportEntry.edition_id == int(edition.id))
            .order_by(DailyEditionReportEntry.display_order.asc(), DailyEditionReportEntry.id.asc())
        )
        if topic:
            stmt = stmt.where(DailyEditionReportEntry.topic == topic)
        if category:
            stmt = stmt.where(
                or_(
                    DailyEditionReportEntry.topic == category,
                    DailyEditionReportEntry.content_class == category,
                )
            )
        if source_group:
            stmt = stmt.where(DailyEditionReportEntry.source_group == source_group)
        if content_class:
            stmt = stmt.where(DailyEditionReportEntry.content_class == content_class)
        normalized_query = query.strip() if query else ""
        if normalized_query:
            like = f"%{normalized_query}%"
            stmt = stmt.where(
                or_(
                    DailyEditionReportEntry.title.ilike(like),
                    DailyEditionReportEntry.original_title.ilike(like),
                    DailyEditionReportEntry.summary.ilike(like),
                    DailyEditionReportEntry.url.ilike(like),
                )
            )
        return list(
            self.session.scalars(
                stmt.offset(max(0, offset)).limit(min(max(limit, 1), 100))
            ).all()
        )

    def _list_daily_report_events(
        self,
        *,
        snapshot: RunSnapshot,
        topic: str | None,
        content_class: str | None,
        limit: int,
    ) -> list[FeaturedEventRow]:
        return [
            self._report_event_row(entry)
            for entry in self._daily_report_entries(
                snapshot=snapshot,
                topic=topic,
                content_class=content_class,
                limit=limit,
            )
        ]

    def _list_daily_report_cards(
        self,
        *,
        snapshot: RunSnapshot,
        category: str | None,
        source_group: str | None,
        content_class: str | None,
        query: str | None,
        offset: int,
        limit: int,
    ) -> list[FeaturedItemRow]:
        cards: list[FeaturedItemRow] = []
        for entry in self._daily_report_entries(
            snapshot=snapshot,
            category=category,
            source_group=source_group,
            content_class=content_class,
            query=query,
            offset=offset,
            limit=limit,
        ):
            row = self._report_event_row(entry)
            cards.append(
                FeaturedItemRow(
                    id=row.event_id,
                    title=row.title,
                    summary=row.summary,
                    selection_reason=f"daily_report:{row.display_order}",
                    url=row.url,
                    risk_note="；".join(row.risk_flags) if row.risk_flags else None,
                    status="selected",
                    selection_score=int(round(row.display_score)),
                    ai_confidence=100,
                    content_class=row.content_class,
                    source_name=row.source_name,
                    source_group=row.source_group,
                    source_subtype=row.source_subtype,
                    risk_flags=row.risk_flags,
                    published_at=row.published_at,
                    created_at=entry.created_at,
                    screen_decision="pass",
                    screen_confidence=100,
                    ai_status="published_daily_report",
                    topic_category=row.topic,
                    source_id=row.source_ids[0] if row.source_ids else None,
                    presentation_labels=row.presentation_labels,
                )
            )
        return cards

    def _report_event_row(self, entry: DailyEditionReportEntry) -> FeaturedEventRow:
        metadata = entry.metadata_dict
        refs: list[dict[str, object]] = []
        source_names: list[str] = []
        for raw_ref in entry.source_refs:
            ref = dict(raw_ref)
            ref["source_url"] = _safe_url(_as_optional_text(ref.get("source_url")))
            refs.append(ref)
            source_name = _as_optional_text(ref.get("source_name"))
            if source_name and source_name not in source_names:
                source_names.append(source_name)
        display_title = _as_optional_text(metadata.get("display_title_zh")) or entry.title
        source_presentation = _as_optional_text(metadata.get("source_presentation"))
        labels = {
            "community_signal_pending_verification": "社区线索 / 待核实",
            "multi_community_signal_pending_verification": "多源社区线索 / 待核实",
        }
        provenance = metadata.get("provenance")
        if isinstance(provenance, dict):
            provenance = provenance.get("kind")
        return FeaturedEventRow(
            event_id=int(entry.id),
            display_order=int(entry.display_order or 0),
            title=display_title,
            original_title=entry.original_title or entry.title,
            summary=entry.summary,
            url=_safe_url(entry.url),
            display_score=float(entry.display_score or 0.0),
            topic=entry.topic,
            content_class=entry.content_class,
            source_name="、".join(source_names) or (entry.source_ids[0] if entry.source_ids else None),
            source_group=entry.source_group,
            source_subtype=None,
            source_ids=tuple(entry.source_ids),
            risk_flags=list(entry.risk_flags),
            published_at=entry.published_at,
            keywords=tuple(entry.keywords),
            entities=tuple(value for value in entry.entities if isinstance(value, dict)),
            provenance=str(provenance or "new"),
            source_refs=tuple(refs),
            story_family_id=_as_optional_text(metadata.get("story_family_id")),
            family_position=_as_optional_int(metadata.get("family_position")),
            presentation_labels=(labels[source_presentation],) if source_presentation in labels else (),
        )

    def _report_member_rows(self, entry: DailyEditionReportEntry) -> tuple[EventMemberRow, ...]:
        rows: list[EventMemberRow] = []
        for position, ref in enumerate(entry.source_refs, start=1):
            item_id = _as_optional_int(ref.get("item_id")) or -(int(entry.id) * 1000 + position)
            rows.append(
                EventMemberRow(
                    item_id=item_id,
                    title=_as_optional_text(ref.get("title")) or entry.original_title or entry.title,
                    summary=entry.summary,
                    url=_safe_url(_as_optional_text(ref.get("source_url"))) or _safe_url(entry.url),
                    published_at=entry.published_at,
                    captured_at=None,
                    source_id=_as_optional_text(ref.get("source_id")),
                    source_name=_as_optional_text(ref.get("source_name")),
                    source_group=_as_optional_text(ref.get("source_group")) or entry.source_group,
                    source_url=_safe_url(_as_optional_text(ref.get("source_url"))),
                    is_primary=bool(ref.get("is_primary")),
                    match_type=_as_optional_text(ref.get("match_type")),
                    match_confidence=_as_optional_int(ref.get("match_confidence")),
                    screen_decision=None,
                    screen_reason_code=None,
                    screen_reason=None,
                    screen_confidence=None,
                    screen_risk_flags=(),
                    review_status=None,
                    review_topic=entry.topic,
                    review_summary=entry.summary,
                    review_reason=None,
                    review_score=None,
                    review_confidence=None,
                    review_risk_flags=(),
                )
            )
        return tuple(rows)

    def _event_row(
        self,
        snapshot: IntelEventStageDSnapshot,
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
        source_refs = tuple(
            {
                "item_id": relation.item_id,
                "source_id": relation.source_id,
                "source_name": (relation.source.name if relation.source is not None else None),
                "source_url": _safe_url(relation.source_url),
                "match_type": relation.match_type,
                "match_confidence": relation.match_confidence,
                "is_primary": bool(relation.is_primary),
            }
            for relation in event.event_items
        )
        try:
            event_entities = json.loads(event.entities_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            event_entities = []
        metadata = _json_mapping(snapshot.metadata_json)
        display_title = str(metadata.get("display_title_zh") or "").strip() or event.title
        source_presentation = str(metadata.get("source_presentation") or "")
        labels = {
            "community_signal_pending_verification": "社区线索 / 待核实",
            "multi_community_signal_pending_verification": "多源社区线索 / 待核实",
        }
        return FeaturedEventRow(
            event_id=int(event.id),
            display_order=int(snapshot.display_order or 0),
            title=display_title,
            original_title=event.title,
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
            keywords=tuple(_json_list(event.keywords_json)),
            entities=tuple(value for value in event_entities if isinstance(value, dict)),
            provenance="new" if event.new_in_run_id == snapshot.run_id else "repeat",
            source_refs=source_refs,
            story_family_id=str(metadata.get("story_family_id") or "") or None,
            family_position=_as_optional_int(metadata.get("family_position")),
            presentation_labels=(labels[source_presentation],) if source_presentation in labels else (),
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
            .where(IntelItem.status.is_not(None))
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
        if filters.topic_category:
            stmt = stmt.where(or_(AIItemReview.topic == filters.topic_category.strip(), AIItemReview.topic.is_(None)))
        if filters.status:
            stmt = stmt.where(IntelItem.status == filters.status.strip())
        if filters.screen_decision:
            stmt = stmt.join(AIItemScreen, AIItemScreen.item_id == IntelItem.id).where(
                AIItemScreen.decision == filters.screen_decision.strip()
            )

        items: list[AllItemRow] = []
        for item, source, review in self.session.execute(stmt).all():
            display_category = _display_topic_category(review.topic if review else None, item, source, self.topic_categories)
            if filters.topic_category and display_category != filters.topic_category.strip():
                continue
            screen = self.session.get(AIItemScreen, item.id)
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
                    screen_decision=screen.decision if screen else None,
                    screen_confidence=screen.confidence if screen else None,
                    screen_reason_code=screen.reason_code if screen else None,
                    screen_reason=screen.reason if screen else None,
                    screen_risk_flags=screen.risk_flags if screen else [],
                    summary_cn=(review.summary_cn if review else None) or item.summary,
                    published_at=item.published_at,
                    fetched_at=item.captured_at,
                    content_class=item.content_class,
                    selection_reason=item.selection_reason,
                    ai_status=review.status if review else None,
                    topic_category=display_category,
                    source_id=source.id,
                    source_transport=source.transport,
                    source_tier=source.tier,
                    source_url=_safe_url(source.url),
                    account_url=_safe_url(source.account_url),
                )
            )
        return items

    def search_content(
        self,
        query: str,
        *,
        snapshot: RunSnapshot | None = None,
        edition_date: str | None = None,
        limit_per_group: int = 8,
    ) -> SearchContentResults:
        normalized_query = query.strip()
        if not normalized_query:
            return SearchContentResults.empty()
        safe_limit = min(max(limit_per_group, 1), 20)
        active_snapshot = snapshot or self.resolve_snapshot(edition_date=edition_date)
        selected_items = [
            SearchResultRow(
                result_type="event",
                id=row.id,
                title=row.title,
                summary=row.summary,
                url=row.url,
                source_name=row.source_name,
                item_id=None,
                score=row.selection_score,
                badges=[badge for badge in ["selected", row.content_class, row.source_group] if badge],
                published_at=row.published_at,
                created_at=row.created_at,
                content_class=row.content_class,
                status="selected",
                ai_status=row.ai_status,
                selection_reason=row.selection_reason,
                topic_category=row.topic_category,
                source_group=row.source_group,
                source_transport=row.source_transport,
                source_tier=row.source_tier,
            )
            for row in self.list_featured_cards(
                snapshot=active_snapshot,
                query=normalized_query,
                limit=safe_limit,
            )
        ]
        return SearchContentResults(
            query=normalized_query,
            selected_items=selected_items,
            items=[],
        )

    def _snapshot_conditions(self, snapshot: RunSnapshot | None) -> tuple[Any, ...]:
        if snapshot is None:
            return (false(),)
        conditions: list[Any] = [
            IntelEventStageDSnapshot.snapshot_key == snapshot.snapshot_key,
            IntelEventStageDSnapshot.selected.is_(True),
        ]
        if snapshot.run_id is None:
            conditions.append(IntelEventStageDSnapshot.run_id.is_(None))
        else:
            conditions.append(IntelEventStageDSnapshot.run_id == snapshot.run_id)
        return tuple(conditions)

    def _search_selected_items(self, *, like: str, limit: int) -> list[SearchResultRow]:
        stmt = (
            select(IntelItem, Source, AIItemReview)
            .join(Source, IntelItem.source_id == Source.id)
            .join(AIItemReview, AIItemReview.item_id == IntelItem.id)
            .where(IntelItem.status == "candidate", AIItemReview.status == "success")
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
            display_category = _display_topic_category(review.topic if review else None, item, source, self.topic_categories)
            rows.append(
                SearchResultRow(
                    result_type="candidate",
                    id=item.id,
                    title=item.title,
                    summary=(review.summary_cn if review else None) or item.summary,
                    url=_safe_url(item.canonical_url),
                    source_name=source.name,
                    item_id=item.id,
                    score=item.selection_score,
                    badges=[badge for badge in ["candidate", item.content_class, item.status] if badge],
                    published_at=item.published_at,
                    created_at=item.created_at,
                    content_class=item.content_class,
                    status=item.status,
                    ai_status=review.status if review else None,
                    selection_reason=item.selection_reason or (review.reason if review else None),
                    topic_category=display_category,
                    source_group=source.source_group,
                    source_transport=source.transport,
                    source_tier=source.tier,
                )
            )
        return rows

    def _count(self, model: Any, *conditions: Any) -> int:
        stmt = select(func.count()).select_from(model)
        if conditions:
            stmt = stmt.where(*conditions)
        return int(self.session.execute(stmt).scalar_one())

    def list_sources(self) -> list[SourceRow]:
        """Return a compact source catalog for the provenance page."""

        counts = {
            source_id: int(count)
            for source_id, count in self.session.execute(
                select(IntelItem.source_id, func.count(IntelItem.id)).group_by(IntelItem.source_id)
            ).all()
        }
        rows = self.session.scalars(select(Source).order_by(Source.priority.asc(), Source.name.asc())).all()
        return [
            SourceRow(
                source_id=source.id,
                name=source.name,
                transport=source.transport,
                source_group=source.source_group,
                source_subtype=source.source_subtype,
                source_role=source.source_role,
                tier=source.tier,
                content_class=source.content_class,
                url=_safe_url(source.url),
                account_url=_safe_url(source.account_url),
                health_status=source.health_status,
                consecutive_failures=int(source.consecutive_failures or 0),
                item_count=counts.get(source.id, 0),
            )
            for source in rows
        ]


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


def _json_mapping(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _as_optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _as_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_edition_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def _sort_timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    try:
        return value.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


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


def _display_topic_category(
    value: str | None,
    item: IntelItem,
    source: Source,
    categories: tuple[str, ...] = DEFAULT_AI_REVIEW_CATEGORIES,
) -> str:
    """Give legacy rows the same readable topic labels as newly reviewed rows."""

    if value and value != "未分类":
        return value
    return fallback_topic_category(
        title=item.title,
        summary=item.summary,
        content=item.content_text,
        source_group=source.source_group,
        source_subtype=source.source_subtype,
        transport=source.transport,
        content_class=source.content_class,
        categories=categories,
    )
