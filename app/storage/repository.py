"""Persistence boundary for the simplified intelligence pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.domain.models import SourceSpec
from app.storage.models import (
    AIItemScreen,
    AIItemReview,
    DailyEdition,
    DailyEditionReportEntry,
    IntelEvent,
    IntelEventItem,
    IntelEventStageDSnapshot,
    IntelItem,
    IntelRun,
    IntelRunItem,
    IntelRunStage,
    IntelRunStageAttempt,
    IntelRunStageTask,
    Source,
    FetchAttempt,
    utcnow,
)


@dataclass(frozen=True)
class IntelInsertResult:
    inserted: bool
    item_id: int | None = None
    reason: str | None = None
    updated: bool = False


@dataclass(frozen=True)
class IntelCounts:
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    screened: int = 0
    screened_out: int = 0
    screen_failed: int = 0
    analysis_filtered: int = 0
    analysis_failed: int = 0
    candidate: int = 0
    partial: int = 0
    selected: int = 0
    analyzed: int = 0
    failed: int = 0


@dataclass(frozen=True)
class EventItemUpsertResult:
    """Result wrapper retained for callers that need created/updated flags."""

    relation: IntelEventItem
    created: bool

    @property
    def event_item(self) -> IntelEventItem:
        return self.relation


@dataclass(frozen=True)
class EventStageDSnapshotUpsertResult:
    snapshot: IntelEventStageDSnapshot
    created: bool


@dataclass(frozen=True)
class StageStateSummary:
    """Small serializable summary used by status/CLI callers."""

    stage_id: int
    run_id: int
    stage_name: str
    status: str
    total: int = 0
    pending: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    retry_waiting: int = 0
    blocked: int = 0


STAGE_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "blocked", "cancelled", "skipped"})
STAGE_RETRYABLE_STATUSES = frozenset({"pending", "retry_waiting", "failed"})
STAGE_RUNNING_STATUSES = frozenset({"running", "in_progress"})
TASK_REUSABLE_STATUS = "succeeded"
DAILY_EDITION_TIMEZONE = ZoneInfo("Asia/Shanghai")

RUN_ITEM_ROLE_FETCHED = "fetched"


class IntelRepository:
    """Small, explicit write/read API used by v2 jobs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_source(self, source: SourceSpec, *, policy: Any | None = None) -> Source:
        """Persist one resolved ``SourceSpec`` without reconstructing config.

        The nested feed/GitHub options are intentionally stored as explicit
        columns. This keeps source rows useful to the fetch cooldown checks and
        lets a fresh local database be inspected without obsolete routing fields.
        """
        row = self.session.get(Source, source.id)
        if row is None:
            row = Source(id=source.id)
            self.session.add(row)
        row.name = source.name or source.id
        row.transport = source.transport
        row.url = source.url
        row.enabled = source.enabled
        row.priority = source.priority
        row.fetch_interval = source.fetch_interval
        row.default_limit = source.default_limit
        feed = source.feed
        github = source.github
        row.feed_format = getattr(feed, "format", None) if feed is not None else None
        row.feed_adapter = getattr(feed, "adapter", None) if feed is not None else None
        row.github_mode = getattr(github, "mode", None) if github is not None else None
        row.github_query = getattr(github, "query", None) if github is not None else None
        row.github_sort = getattr(github, "sort", None) if github is not None else None
        row.github_order = getattr(github, "order", None) if github is not None else None
        row.github_pushed_days = getattr(github, "pushed_days", None) if github is not None else None
        row.github_period = getattr(github, "period", None) if github is not None else None
        row.source_group = source.source_group or "general"
        row.source_subtype = source.source_subtype or "fixed"
        row.account_url = source.account_url
        row.tier = getattr(source, "tier", None) or "p4"
        row.topic_scopes_json = _dump_json(list(getattr(source, "topic_scopes", ()) or ()))
        row.primary_eligible = bool(getattr(source, "primary_eligible", False))
        row.quality_weight = source.quality_weight
        row.source_role = source.source_role
        row.spam_risk = source.spam_risk
        row.content_class = source.content_class or "community_social"
        selection_policy = getattr(source, "selection_policy", {})
        if hasattr(selection_policy, "model_dump"):
            selection_policy = selection_policy.model_dump(exclude_none=True)
        row.selection_policy_json = _dump_json(selection_policy or {})
        if policy is not None:
            # Resolved ``SourceSpec`` instances normally provide both values,
            # but callers may pass a lightweight policy object. Preserve the
            # source defaults so a missing optional attribute cannot violate
            # the non-null schema on commit.
            row.content_class = (
                _text(getattr(policy, "content_class", None))
                or source.content_class
                or row.content_class
                or "community_social"
            )
            row.selection_policy_json = _dump_json(_policy_dict(policy, "selection"))
        return row

    def update_source_health(
        self,
        source_id: str,
        *,
        success: bool,
        error_code: str | None = None,
        error_message: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        now: datetime | None = None,
        backoff_base_seconds: int = 60,
        max_backoff_seconds: int = 86_400,
    ) -> Source | None:
        """Persist source health/backoff state without failing the batch."""

        # ``upsert_source`` intentionally leaves the transaction open for
        # callers that batch registry writes. Flush here so a health update in
        # the same transaction can resolve a newly-created Source row too.
        self.session.flush()
        row = self.session.get(Source, source_id)
        if row is None:
            return None
        current = now or utcnow()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if success:
            row.health_status = "healthy"
            row.consecutive_failures = 0
            row.backoff_until = None
            row.last_error_code = None
            row.last_error_message = None
            row.last_fetched_at = current
            if etag:
                row.etag = str(etag)[:512]
            if last_modified:
                row.last_modified = str(last_modified)[:255]
        else:
            failures = int(row.consecutive_failures or 0) + 1
            row.health_status = "failed"
            row.consecutive_failures = failures
            row.last_error_code = str(error_code or "fetch_failed")[:128]
            row.last_error_message = str(error_message or "")[:4000] or None
            delay = min(max_backoff_seconds, backoff_base_seconds * (2 ** max(0, failures - 1)))
            row.backoff_until = current + timedelta(seconds=delay)
            row.last_fetched_at = current
        row.updated_at = current
        self.session.flush()
        return row

    def list_source_health(self, *, source_id: str | None = None) -> list[Source]:
        stmt = select(Source).order_by(Source.priority.asc(), Source.id.asc())
        if source_id:
            stmt = stmt.where(Source.id == source_id)
        return list(self.session.scalars(stmt).all())

    # ------------------------------------------------------------------
    # Date-addressed report rows and draft-workspace builds
    # ------------------------------------------------------------------

    def get_daily_edition(self, edition_date: date | str) -> DailyEdition | None:
        resolved = _as_edition_date(edition_date)
        if resolved is None:
            raise ValueError("edition_date must use YYYY-MM-DD")
        return self.session.scalar(
            select(DailyEdition).where(DailyEdition.edition_date == resolved)
        )

    def get_or_create_daily_edition(self, edition_date: date | str) -> DailyEdition:
        resolved = _as_edition_date(edition_date)
        if resolved is None:
            raise ValueError("edition_date must use YYYY-MM-DD")
        edition = self.get_daily_edition(resolved)
        if edition is None:
            edition = DailyEdition(edition_date=resolved)
            self.session.add(edition)
            self.session.flush()
        return edition

    def draft_run_for_edition(self, edition_date: date | str) -> IntelRun | None:
        edition = self.get_daily_edition(edition_date)
        if edition is None:
            return None
        return self.session.scalar(
            select(IntelRun)
            .where(IntelRun.edition_id == int(edition.id))
            .order_by(IntelRun.id.desc())
        )

    def start_daily_build(
        self,
        *,
        edition_date: date | str,
        filters: Mapping[str, Any] | None = None,
        scope: Mapping[str, Any] | None = None,
        source_ids: Iterable[str] | None = None,
        reference_time: datetime | None = None,
    ) -> tuple[DailyEdition, IntelRun]:
        """Create the one fresh build inside an isolated draft workspace.

        This method must be called with the date's pending draft database,
        never the published report database.  Replacing an existing draft is
        therefore safe: only that pending build's raw items and A-D state are
        deleted; the prior retained ``audit.db`` is outside this database.
        """

        edition = self.get_or_create_daily_edition(edition_date)
        stale_build_ids = [
            int(run_id)
            for run_id in self.session.scalars(
                select(IntelRun.id).where(IntelRun.edition_id == int(edition.id))
            ).all()
        ]
        for stale_build_id in stale_build_ids:
            self.delete_build(stale_build_id)
        if stale_build_ids:
            self.session.flush()

        source_values = _unique_strings(source_ids or ())
        scope_values = dict(scope or {})
        reference = _as_utc(reference_time) or _as_utc(scope_values.get("reference_time")) or utcnow()
        scope_values["reference_time"] = reference.isoformat()
        run = IntelRun(
            edition_id=int(edition.id),
            status="running",
            filters_json=_dump_json(dict(filters or {})),
            scope_json=_dump_json(scope_values),
            source_ids_json=_dump_json(source_values),
        )
        run.edition = edition
        self.session.add(run)
        self.session.flush()
        edition.status = "building"
        edition.error = None
        self.session.flush()
        return edition, run

    def mark_daily_build_failed(self, run_id: int, *, error: str | None = None) -> DailyEdition | None:
        run = self.session.get(IntelRun, int(run_id))
        if run is None or run.edition_id is None:
            return None
        edition = self.session.get(DailyEdition, int(run.edition_id))
        current = self.draft_run_for_edition(edition.edition_date) if edition is not None else None
        if edition is None or current is None or int(current.id) != int(run_id):
            return edition
        edition.status = "draft_failed"
        edition.error = _text(error) or run.error
        self.session.flush()
        return edition

    def mark_daily_build_partial(self, run_id: int, *, error: str | None = None) -> DailyEdition | None:
        """Keep a completed-but-incomplete draft without publishing it."""

        run = self.session.get(IntelRun, int(run_id))
        if run is None or run.edition_id is None:
            return None
        edition = self.session.get(DailyEdition, int(run.edition_id))
        current = self.draft_run_for_edition(edition.edition_date) if edition is not None else None
        if edition is None or current is None or int(current.id) != int(run_id):
            return edition
        edition.status = "draft_partial"
        edition.error = _text(error) or run.partial_reason or run.error
        self.session.flush()
        return edition

    def replace_published_daily_report(
        self,
        *,
        edition_date: date | str,
        records: Iterable[Mapping[str, Any]],
    ) -> DailyEdition:
        """Replace one public date's final report without touching any build.

        The formal database intentionally stores only published date-level
        content.  A draft workspace calls this once its own export has been
        approved and its output directory is ready to promote.
        """

        edition = self.get_or_create_daily_edition(edition_date)
        self._replace_daily_report_entries(edition, records)
        edition.status = "published"
        edition.published_at = utcnow()
        edition.error = None
        self.session.flush()
        return edition

    def _replace_daily_report_entries(
        self,
        edition: DailyEdition,
        records: Iterable[Mapping[str, Any]],
    ) -> None:
        """Overwrite only compact final report rows for one date."""

        for entry in list(edition.report_entries):
            self.session.delete(entry)
        self.session.flush()
        for order, record in enumerate(records, start=1):
            source_ids = record.get("source_ids") if isinstance(record.get("source_ids"), list) else []
            source_refs = record.get("source_refs") if isinstance(record.get("source_refs"), list) else []
            risk_flags = record.get("risk_flags") if isinstance(record.get("risk_flags"), list) else []
            keywords = record.get("keywords") if isinstance(record.get("keywords"), list) else []
            entities = record.get("entities") if isinstance(record.get("entities"), list) else []
            metadata = dict(record.get("metadata") or {}) if isinstance(record.get("metadata"), Mapping) else {}
            # The final report is the only durable UI/history payload after
            # raw build rows are deleted. Preserve the small public editorial
            # fields that used to live beside ``metadata`` on the export row.
            for key in (
                "reason",
                "provenance",
                "story_family_id",
                "family_position",
                "editorial_score",
                "presentation_labels",
            ):
                if key in record and key not in metadata:
                    metadata[key] = record[key]
            event_key = _text(record.get("event_key"))
            if event_key is None:
                raise ValueError("published daily report record is missing event_key")
            self.session.add(
                DailyEditionReportEntry(
                    edition_id=int(edition.id),
                    event_key=event_key[:512],
                    # The final report owns a dense, date-local order.  Do
                    # not let malformed provider output violate the unique
                    # `(edition_id, display_order)` constraint.
                    display_order=order,
                    title=_text(record.get("title")) or "(untitled)",
                    original_title=_text(record.get("original_title")) or _text(record.get("title")),
                    summary=_text(record.get("summary_cn")) or _text(record.get("summary")),
                    url=_text(record.get("url")) or _text(record.get("canonical_url")),
                    display_score=float(record.get("display_score") or record.get("selection_score") or 0.0),
                    topic=_text(record.get("topic")) or _text(record.get("topic_category")),
                    content_class=_text(record.get("content_class")),
                    source_group=_text(record.get("source_group")),
                    source_ids_json=_dump_json(source_ids),
                    source_refs_json=_dump_json(source_refs),
                    risk_flags_json=_dump_json(risk_flags),
                    keywords_json=_dump_json(keywords),
                    entities_json=_dump_json(entities),
                    metadata_json=_dump_json(dict(metadata)),
                    published_at=_as_utc(record.get("published_at")),
                )
            )

    def list_daily_report_entries(
        self,
        edition_date: date | str,
        *,
        limit: int | None = None,
    ) -> list[DailyEditionReportEntry]:
        edition = self.get_daily_edition(edition_date)
        if edition is None:
            return []
        stmt = (
            select(DailyEditionReportEntry)
            .where(DailyEditionReportEntry.edition_id == int(edition.id))
            .order_by(DailyEditionReportEntry.display_order.asc(), DailyEditionReportEntry.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def list_prior_daily_report_entries(
        self,
        *,
        edition_date: date | str,
        days: int,
    ) -> list[DailyEditionReportEntry]:
        current = _as_edition_date(edition_date)
        if current is None or days <= 0:
            return []
        earliest = current - timedelta(days=int(days))
        stmt = (
            select(DailyEditionReportEntry)
            .join(DailyEdition, DailyEdition.id == DailyEditionReportEntry.edition_id)
            .where(
                DailyEdition.edition_date >= earliest,
                DailyEdition.edition_date < current,
                DailyEdition.published_at.is_not(None),
            )
            .order_by(DailyEdition.edition_date.desc(), DailyEditionReportEntry.display_order.asc())
        )
        return list(self.session.scalars(stmt).all())

    def delete_build(self, run_id: int) -> None:
        """Hard-delete one hidden build and every raw/intermediate descendant."""

        run = self.session.get(IntelRun, int(run_id))
        if run is None:
            return
        # Stage tasks refer to items/events and attempts refer back to tasks.
        # Delete them first so SQLite installations with FK enforcement enabled
        # can purge an entire build without depending on implicit cascades.
        for stage in list(self.session.scalars(select(IntelRunStage).where(IntelRunStage.run_id == int(run_id))).all()):
            for task in list(self.session.scalars(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage.id)).all()):
                task.last_attempt_id = None
                for attempt in list(
                    self.session.scalars(select(IntelRunStageAttempt).where(IntelRunStageAttempt.task_id == task.id)).all()
                ):
                    self.session.delete(attempt)
                self.session.delete(task)
            self.session.delete(stage)

        item_ids = [
            int(value)
            for value in self.session.scalars(
                select(IntelItem.id).where(IntelItem.build_id == int(run_id))
            ).all()
        ]
        event_ids = [
            int(value)
            for value in self.session.scalars(
                select(IntelEvent.id).where(
                    IntelEvent.build_id == int(run_id)
                )
            ).all()
        ]
        if item_ids:
            event_ids.extend(
                int(value)
                for value in self.session.scalars(
                    select(IntelEventItem.event_id).where(IntelEventItem.item_id.in_(item_ids))
                ).all()
            )
        event_ids = list(dict.fromkeys(event_ids))

        snapshot_conditions = [IntelEventStageDSnapshot.run_id == int(run_id)]
        if event_ids:
            snapshot_conditions.append(IntelEventStageDSnapshot.event_id.in_(event_ids))
        for snapshot in list(self.session.scalars(select(IntelEventStageDSnapshot).where(or_(*snapshot_conditions))).all()):
            self.session.delete(snapshot)
        if event_ids:
            for relation in list(
                self.session.scalars(select(IntelEventItem).where(IntelEventItem.event_id.in_(event_ids))).all()
            ):
                self.session.delete(relation)
            for event in list(self.session.scalars(select(IntelEvent).where(IntelEvent.id.in_(event_ids))).all()):
                self.session.delete(event)
        for item_id in item_ids:
            item = self.session.get(IntelItem, item_id)
            if item is None:
                continue
            screen = self.session.scalar(select(AIItemScreen).where(AIItemScreen.item_id == item_id))
            review = self.session.scalar(select(AIItemReview).where(AIItemReview.item_id == item_id))
            if screen is not None:
                self.session.delete(screen)
            if review is not None:
                self.session.delete(review)
            self.session.delete(item)
        for relation in list(self.session.scalars(select(IntelRunItem).where(IntelRunItem.run_id == int(run_id))).all()):
            self.session.delete(relation)
        for attempt in list(self.session.scalars(select(FetchAttempt).where(FetchAttempt.run_id == int(run_id))).all()):
            self.session.delete(attempt)
        self.session.flush()
        self.session.delete(run)
        self.session.flush()

    def freeze_run_scope(
        self,
        run_id: int,
        *,
        source_ids: Iterable[str] | None = None,
        item_ids: Iterable[int] | None = None,
        scope: Mapping[str, Any] | None = None,
        frozen_at: datetime | None = None,
    ) -> IntelRun | None:
        """Freeze fetch membership before any resumable processing stage.

        A frozen build may still update ``IntelRunItem.status`` projections,
        but cannot gain new source/item membership.
        """

        run = self.session.get(IntelRun, int(run_id))
        if run is None:
            return None
        if run.scope_frozen:
            # Idempotent re-entry is safe only when no caller asks to mutate
            # the already-frozen membership or reserved metadata.
            if source_ids is not None or item_ids is not None or scope:
                raise RuntimeError(f"intel run {run_id} scope is already frozen")
            return run
        if source_ids is not None:
            run.source_ids_json = _dump_json(_unique_strings(source_ids))
        elif not run.source_ids:
            scoped_sources = self.session.scalars(
                select(IntelRunItem.source_id).where(
                    IntelRunItem.run_id == run.id,
                    IntelRunItem.source_id.is_not(None),
                )
            ).all()
            run.source_ids_json = _dump_json(_unique_strings(scoped_sources))
        if item_ids is not None:
            run.item_ids_json = _dump_json(_unique_ints(item_ids))
        elif not run.item_ids:
            run.item_ids_json = _dump_json(self.list_run_item_ids(run.id))
        values = run.scope
        if scope:
            values.update(dict(scope))
        current = _as_utc(frozen_at) or utcnow()
        values["_frozen"] = True
        values["_frozen_at"] = current.isoformat()
        # Preserve the run reference even if an old row did not have it.
        values.setdefault("reference_time", (_as_utc(run.started_at) or current).isoformat())
        run.scope_json = _dump_json(values)
        self.session.flush()
        return run

    def set_run_scope(
        self,
        run_id: int,
        *,
        source_ids: Iterable[str] | None = None,
        item_ids: Iterable[int] | None = None,
        scope: Mapping[str, Any] | None = None,
    ) -> IntelRun | None:
        run = self.session.get(IntelRun, int(run_id))
        if run is None:
            return None
        if run.scope_frozen and (source_ids is not None or item_ids is not None or scope):
            raise RuntimeError(f"intel run {run_id} scope is already frozen")
        if source_ids is not None:
            run.source_ids_json = _dump_json(_unique_strings(source_ids))
        if item_ids is not None:
            run.item_ids_json = _dump_json(_unique_ints(item_ids))
        if scope is not None:
            existing = _load_json(run.scope_json, {})
            existing = dict(existing) if isinstance(existing, Mapping) else {}
            existing.update(dict(scope))
            run.scope_json = _dump_json(existing)
        self.session.flush()
        return run

    def record_run_item(
        self,
        run_id: int,
        item_id: int,
        *,
        source_id: str | None = None,
        status: str = "fetched",
    ) -> IntelRunItem:
        """Attach an item to a run scope idempotently.

        This method performs only serial SQLAlchemy work; provider threads must
        return their result before the job invokes it.
        """

        run = self.session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        item = self.session.get(IntelItem, int(item_id))
        if item is None:
            raise ValueError(f"intel item {item_id} does not exist")
        build_id = self._daily_build_id(run_id)
        if build_id is not None and item.build_id != build_id:
            raise ValueError(
                f"intel item {item_id} does not belong to daily build {run_id}"
            )
        relation = self.session.scalar(
            select(IntelRunItem).where(
                IntelRunItem.run_id == int(run_id), IntelRunItem.item_id == int(item_id)
            )
        )
        if relation is None:
            if run.scope_frozen:
                raise RuntimeError(f"intel run {run_id} scope is already frozen")
            relation = IntelRunItem(run_id=int(run_id), item_id=int(item_id))
            self.session.add(relation)
        relation.source_id = source_id or relation.source_id or item.source_id
        relation.role = RUN_ITEM_ROLE_FETCHED
        relation.status = _text(status) or "fetched"
        item_ids = _unique_ints([*(_string_values(_load_json(run.item_ids_json, []))), int(item_id)])
        run.item_ids_json = _dump_json(item_ids)
        relation.updated_at = utcnow()
        self.session.flush()
        return relation

    def update_run_item_status(self, run_id: int, item_id: int, *, status: str) -> IntelRunItem | None:
        relation = self.session.scalar(
            select(IntelRunItem).where(
                IntelRunItem.run_id == int(run_id), IntelRunItem.item_id == int(item_id)
            )
        )
        if relation is None:
            return None
        relation.status = _text(status) or relation.status
        relation.updated_at = utcnow()
        self.session.flush()
        return relation

    def list_run_item_ids(
        self,
        run_id: int,
        *,
        role: str | Iterable[str] | None = None,
        status: str | Iterable[str] | None = None,
    ) -> list[int]:
        stmt = select(IntelRunItem.item_id).where(IntelRunItem.run_id == int(run_id)).order_by(IntelRunItem.id.asc())
        if role:
            stmt = stmt.where(IntelRunItem.role.in_(_run_item_roles(role)))
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            stmt = stmt.where(IntelRunItem.status.in_(statuses))
        return [int(value) for value in self.session.scalars(stmt).all()]

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        counts: IntelCounts | Mapping[str, int] | None = None,
        error: str | None = None,
        partial: bool | None = None,
        partial_reason: str | None = None,
    ) -> None:
        run = self.session.get(IntelRun, run_id)
        if run is None:
            return
        values = _counts_dict(counts)
        run.status = status
        run.finished_at = utcnow()
        for name in (
            "fetched", "inserted", "screened", "screened_out", "screen_failed",
            "analyzed", "analysis_filtered", "analysis_failed", "candidate", "failed",
        ):
            if name in values:
                setattr(run, name, int(values[name]))
        if "selected" in values:
            run.selected = int(values["selected"])
        elif "candidate" in values:
            run.selected = int(values["candidate"])
        if partial is not None:
            run.partial = bool(partial)
        elif "partial" in values:
            run.partial = bool(values["partial"])
        if partial_reason is not None:
            run.partial_reason = _text(partial_reason)
        elif values.get("partial_reason") is not None:
            run.partial_reason = _text(values.get("partial_reason"))
        run.error = error[:4000] if error else None
        self.session.flush()

    def create_attempt(
        self,
        *,
        source_id: str,
        request_url: str,
        run_id: int,
        manual_override: bool = False,
    ) -> FetchAttempt:
        self._daily_build_id(run_id)
        attempt = FetchAttempt(
            source_id=source_id,
            run_id=int(run_id),
            request_url=request_url,
            manual_override=manual_override,
            started_at=utcnow(),
            status="running",
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        metadata: Any | None = None,
        items_fetched: int = 0,
        items_inserted: int = 0,
        items_skipped: int = 0,
        error: Exception | str | None = None,
    ) -> None:
        attempt = self.session.get(FetchAttempt, attempt_id)
        if attempt is None:
            return
        attempt.status = status
        attempt.finished_at = utcnow()
        attempt.items_fetched = items_fetched
        attempt.items_inserted = items_inserted
        attempt.items_skipped = items_skipped
        if metadata is not None:
            attempt.transport = getattr(metadata, "transport", None) or attempt.transport
            request_url = getattr(metadata, "request_url", None)
            if request_url:
                attempt.request_url = str(request_url)[:2000]
            attempt.final_url = getattr(metadata, "final_url", None)
            attempt.http_status = getattr(metadata, "http_status", None)
            if attempt.http_status is None:
                attempt.http_status = getattr(metadata, "status_code", None)
            attempt.response_bytes = int(getattr(metadata, "response_bytes", 0) or 0)
            attempt.retry_count = int(getattr(metadata, "retry_count", 0) or 0)
            attempt.error_code = getattr(metadata, "error_code", None)
        if error is not None:
            message = str(error)
            if isinstance(error, str) and error:
                # Callers use short stable error codes for scheduler skips;
                # preserve those instead of recording the generic ``str``.
                candidate_code = error.split(":", 1)[0].strip()
                attempt.error_code = attempt.error_code or (candidate_code[:64] if candidate_code else "fetch_failed")
            else:
                attempt.error_code = getattr(error, "error_code", None) or attempt.error_code or type(error).__name__.lower()
            attempt.error_message = message[:4000]

    def _daily_build_id(self, run_id: int) -> int:
        """Return the private build namespace for a public daily draft."""

        run = self.session.get(IntelRun, int(run_id))
        if run is None or run.edition_id is None:
            raise ValueError("persistent data requires a current daily edition build")
        return int(run.id)

    def insert_item(self, item: Any, *, run_id: int) -> IntelInsertResult:
        """Insert or refresh one normalized item inside its processing scope.

        A date-addressed build gets a private ``build_id`` namespace, so every
        enabled source response is re-evaluated from scratch.
        """

        fields = _item_fields(item)
        # Collectors may omit the class because the source registry is the
        # authoritative routing boundary. Resolve that class before dedupe and
        # persistence so GitHub rows are not accidentally stored as community
        # items when a lightweight DTO omits it.
        if not fields["content_class"]:
            source = self.session.get(Source, fields["source_id"])
            if source is None:
                # ``upsert_source`` may still be pending in the same
                # transaction (as in direct job/test invocations).
                self.session.flush()
                source = self.session.get(Source, fields["source_id"])
            fields["content_class"] = source.content_class if source is not None else "community_social"
        if fields["content_class"] == "project_tool":
            github_url = _canonical_github_url(fields.get("canonical_url"))
            if github_url and (str(fields.get("external_id") or "").casefold().startswith("github_repo:")):
                fields["canonical_url"] = github_url
        build_id = self._daily_build_id(run_id)
        existing = self._find_existing(fields, build_id=build_id)
        if existing is not None:
            # Refresh volatile metrics/payload for audit. Only a material
            # content change creates daily A/B/C work; metric/payload churn
            # alone must not make an old feed entry a fresh daily candidate.
            old_metrics = existing.metrics_json
            old_payload = existing.raw_payload_json
            merged_metrics = (
                _merge_github_project_metrics(
                    _load_json(old_metrics, {}),
                    fields["metrics"],
                    source_id=fields["source_id"],
                )
                if _is_github_repository_fields(fields)
                else fields["metrics"]
            )
            new_metrics = _dump_json(merged_metrics)
            new_payload = _dump_json(
                _merge_github_raw_payload(_load_json(old_payload, {}), fields["raw_payload"])
                if _is_github_repository_fields(fields)
                else fields["raw_payload"]
            )
            material_change = _item_has_material_change(
                existing,
                fields,
            )
            content_hash_changed = (
                not _is_github_repository_fields(fields)
                and bool(fields["content_hash"])
                and existing.content_hash != fields["content_hash"]
            )
            existing.title = fields["title"] or existing.title
            existing.summary = fields["summary"] or existing.summary
            existing.content_text = fields["content_text"] or existing.content_text
            existing.canonical_url = fields["canonical_url"] or existing.canonical_url
            existing.published_at = fields["published_at"] or existing.published_at
            existing.discovered_at = fields["discovered_at"] or existing.discovered_at
            existing.original_title = fields["original_title"] or existing.original_title
            existing.source_url = fields["source_url"] or existing.source_url
            existing.content_depth = fields["content_depth"] or existing.content_depth
            existing.content_class = fields["content_class"]
            existing.metrics_json = new_metrics
            existing.raw_payload_json = new_payload
            if content_hash_changed and not self.session.scalar(
                select(IntelItem.id).where(
                    IntelItem.content_hash == fields["content_hash"],
                    IntelItem.source_id == fields["source_id"],
                    _build_scope(IntelItem.build_id, build_id),
                    IntelItem.id != existing.id,
                )
            ):
                existing.content_hash = fields["content_hash"]
            if material_change and existing.status in {
                "hotspot",
                "rejected",
                "filtered",
                "ai_failed",
                "screened_out",
                "screen_failed",
                "analysis_filtered",
                "analysis_failed",
                "candidate",
            }:
                # A refreshed item must pass deterministic selection again. Its
                # previous AI result remains available for audit until the
                # next AI review replaces it.
                existing.status = "new"
                existing.selection_score = 0
                existing.selection_reason = None
            existing.updated_at = utcnow()
            self.record_run_item(
                run_id,
                existing.id,
                source_id=existing.source_id,
                status="fetched",
            )
            return IntelInsertResult(inserted=False, item_id=existing.id, reason="duplicate", updated=True)

        row_metrics = (
            _merge_github_project_metrics({}, fields["metrics"], source_id=fields["source_id"])
            if _is_github_repository_fields(fields)
            else fields["metrics"]
        )
        row = IntelItem(
            build_id=build_id,
            source_id=fields["source_id"],
            external_id=fields["external_id"],
            canonical_url=fields["canonical_url"],
            title=fields["title"] or "(untitled)",
            summary=fields["summary"],
            content_text=fields["content_text"],
            published_at=fields["published_at"],
            captured_at=fields["captured_at"],
            discovered_at=fields["discovered_at"],
            original_title=fields["original_title"],
            source_url=fields["source_url"],
            content_depth=fields["content_depth"],
            content_class=fields["content_class"],
            metrics_json=_dump_json(row_metrics),
            raw_payload_json=_dump_json(fields["raw_payload"]),
            content_hash=fields["content_hash"],
            status="new",
        )
        self.session.add(row)
        self.session.flush()
        self.record_run_item(
            run_id,
            row.id,
            source_id=row.source_id,
            status="fetched",
        )
        return IntelInsertResult(inserted=True, item_id=row.id)

    def save_github_enrichment(self, item_id: int, enrichment: Mapping[str, Any]) -> IntelItem | None:
        """Persist bounded repository metadata/README enrichment in JSON fields.

        GitHub rows already carry all required storage columns.  This method
        merges enrichment into metrics/raw payload while preserving period Star
        signals and source provenance, so reruns remain idempotent.
        """

        item = self.session.get(IntelItem, item_id)
        if item is None:
            return None
        metadata = enrichment.get("metadata") if isinstance(enrichment, Mapping) else {}
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        metrics = _load_json(item.metrics_json, {})
        metadata_metrics = _github_metadata_metrics(metadata)
        metadata_metrics["github_enrichment_status"] = "partial" if enrichment.get("errors") else "success"
        metadata_metrics["github_enrichment_errors"] = [str(error) for error in enrichment.get("errors", []) if error]
        metadata_metrics["readme_checked"] = bool(enrichment.get("readme_checked"))
        if enrichment.get("readme_checked"):
            metadata_metrics["readme_present"] = enrichment.get("readme_present")
            metadata_metrics["has_readme"] = enrichment.get("readme_present")
        merged_metrics = _merge_github_project_metrics(metrics, metadata_metrics, source_id=item.source_id)

        readme_text = _text(enrichment.get("readme_text"))
        if readme_text:
            merged_metrics["readme_chars"] = len(readme_text)
            item.content_text = readme_text
        elif not metrics.get("readme_chars") and (
            enrichment.get("readme_checked")
            or "readme_text" in enrichment
            or enrichment.get("errors")
        ):
            # GitHub collectors use the normalized content field for a compact
            # metrics payload before enrichment. Never expose that JSON as a
            # README when the bounded README lookup did not produce text.
            item.content_text = None
        description = _text(metadata.get("description"))
        if description:
            item.summary = description
        item.metrics_json = _dump_json(merged_metrics)

        raw_payload = _load_json(item.raw_payload_json, {})
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        raw_payload = _merge_github_raw_payload(raw_payload, _bounded_github_metadata(metadata))
        raw_payload["github_enrichment"] = {
            "metadata_fetched": bool(metadata),
            "readme_checked": bool(enrichment.get("readme_checked")),
            "readme_present": enrichment.get("readme_present"),
            "readme_text": readme_text,
            "errors": [str(error) for error in enrichment.get("errors", []) if error],
        }
        item.raw_payload_json = _dump_json(raw_payload)
        item.updated_at = utcnow()
        self.session.flush()
        return item

    def _find_existing(self, fields: Mapping[str, Any], *, build_id: int) -> IntelItem | None:
        external_id = fields.get("external_id")
        if external_id:
            stmt = select(IntelItem).where(
                _build_scope(IntelItem.build_id, build_id),
                IntelItem.source_id == fields["source_id"],
                IntelItem.external_id == external_id,
            )
            found = self.session.scalar(stmt)
            if found is not None:
                return found
        content_hash = fields.get("content_hash")
        if content_hash:
            return self.session.scalar(
                select(IntelItem).where(
                    _build_scope(IntelItem.build_id, build_id),
                    IntelItem.source_id == fields["source_id"],
                    IntelItem.content_hash == content_hash,
                )
            )
        return None

    def list_run_items(
        self,
        run_id: int,
        *,
        statuses: Iterable[str] | None = None,
        role: str | Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[IntelItem]:
        """Read only the items attached to one run scope."""

        stmt = (
            select(IntelItem)
            .join(IntelRunItem, IntelRunItem.item_id == IntelItem.id)
            .options(
                joinedload(IntelItem.source),
                joinedload(IntelItem.ai_screen),
                joinedload(IntelItem.ai_review),
            )
            .where(IntelRunItem.run_id == int(run_id))
            .order_by(IntelItem.id.asc())
        )
        if statuses:
            stmt = stmt.where(IntelItem.status.in_(list(statuses)))
        if role:
            stmt = stmt.where(IntelRunItem.role.in_(_run_item_roles(role)))
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).unique().all())

    def upsert_ai_screen(
        self,
        item_id: int,
        response: Any,
        *,
        run_id: int,
        model: str | None = None,
        status: str | None = None,
        error_message: str | None = None,
    ) -> AIItemScreen:
        """Persist one Stage A result, including the untouched raw payload."""

        item = self.session.get(IntelItem, int(item_id))
        if item is None:
            raise ValueError(f"intel item {item_id} does not exist")
        if item.build_id != self._daily_build_id(run_id):
            raise ValueError(f"intel item {item_id} does not belong to daily build {run_id}")
        screen = self.session.scalar(select(AIItemScreen).where(AIItemScreen.item_id == int(item_id)))
        if screen is None:
            screen = AIItemScreen(item_id=int(item_id))
            self.session.add(screen)
        screen.model = model
        screen.decision = _normalize_screen_decision(_response_value(response, "decision", "uncertain"))
        screen.reason_code = _text(_response_value(response, "reason_code") or _response_value(response, "code")) or ""
        screen.reason = _text(_response_value(response, "reason") or _response_value(response, "explanation")) or ""
        screen.confidence = _bounded_int(_response_value(response, "confidence", 0))
        screen.risk_flags_json = _dump_json(_structured_json(_response_value(response, "risk_flags", [])))
        raw = _response_value(response, "raw_response") if response is not None else None
        if raw is None and isinstance(response, Mapping):
            raw = dict(response)
        screen.raw_response_json = _dump_json(_structured_json(raw if raw is not None else {}))
        response_status = _text(_response_value(response, "status"))
        screen.status = response_status or _text(status) or "success"
        screen.error_code = _text(_response_value(response, "error_code"))
        response_error = _text(_response_value(response, "error_message"))
        screen.error_message = (response_error or error_message or "")[:4000] or None
        screen.updated_at = utcnow()
        self.session.flush()
        self.update_run_item_status(run_id, item_id, status=screen.status)
        return screen

    def upsert_ai_analysis(
        self,
        item_id: int,
        response: Any,
        *,
        run_id: int,
        model: str | None = None,
        content_class: str | None = None,
        status: str | None = None,
        error_message: str | None = None,
    ) -> AIItemReview:
        """Persist one Stage B projection and its raw provider payload."""

        item = self.session.get(IntelItem, int(item_id))
        if item is None:
            raise ValueError(f"intel item {item_id} does not exist")
        if item.build_id != self._daily_build_id(run_id):
            raise ValueError(f"intel item {item_id} does not belong to daily build {run_id}")
        review = self.session.scalar(select(AIItemReview).where(AIItemReview.item_id == int(item_id)))
        if review is None:
            review = AIItemReview(
                item_id=int(item_id),
                content_class=content_class or item.content_class or "community_social",
            )
            self.session.add(review)
        review.model = model
        review.content_class = _text(
            _response_value(response, "source_content_class")
            or _response_value(response, "content_class")
            or content_class
            or item.content_class
            or "community_social"
        ) or "community_social"
        review.topic = _text(_response_value(response, "topic"))
        review.topics_json = _dump_json(_structured_json(_response_value(response, "topics", [])))
        review.keywords_json = _dump_json(_structured_json(_response_value(response, "keywords", [])))
        review.entities_json = _dump_json(
            _structured_json(_response_value(response, "entities") or _response_value(response, "typed_entities", []))
        )
        score = _response_value(response, "selection_score")
        review.selection_score = _bounded_int(score) if score is not None else None
        score_components = _response_value(response, "score_components")
        if score_components is None:
            score_components = _response_value(response, "scores", {})
        review.score_components_json = _dump_json(_structured_json(score_components or {}))
        review.summary_cn = _text(_response_value(response, "summary_cn") or _response_value(response, "summary"))
        raw = _response_value(response, "raw_response") if response is not None else None
        if raw is None and isinstance(response, Mapping):
            raw = dict(response)
        review.raw_response_json = _dump_json(_structured_json(raw if raw is not None else {}))
        response_status = _text(_response_value(response, "status"))
        review.status = response_status or _text(status) or "success"
        review.error_code = _text(_response_value(response, "error_code"))
        response_error = _text(_response_value(response, "error_message"))
        review.error_message = (response_error or error_message or "")[:4000] or None
        review.updated_at = utcnow()
        self.session.flush()
        self.update_run_item_status(run_id, item_id, status=review.status)
        return review

    def set_item_status(self, item_id: int, status: str, *, run_id: int) -> None:
        item = self.session.get(IntelItem, item_id)
        if item is not None:
            if item.build_id != self._daily_build_id(run_id):
                raise ValueError(f"intel item {item_id} does not belong to daily build {run_id}")
            item.status = status
            item.updated_at = utcnow()
            self.update_run_item_status(run_id, item_id, status=status)

    # ------------------------------------------------------------------
    # Event aggregation and Stage-D snapshot persistence
    # ------------------------------------------------------------------

    def upsert_event(
        self,
        *,
        run_id: int,
        event_key: str,
        canonical_url: str | None = None,
        external_id: str | None = None,
        normalized_title: str | None = None,
        title: str | None = None,
        summary_cn: str | None = None,
        topic: str | None = None,
        topics: Iterable[str] | None = None,
        keywords: Iterable[str] | None = None,
        entities: Iterable[Mapping[str, Any]] | None = None,
        content_class: str | None = None,
        source_group: str | None = None,
        source_ids: Iterable[str] | None = None,
        source_groups: Iterable[str] | None = None,
        identity_keys: Iterable[str] | None = None,
        display_score: float | None = None,
        novelty_status: str | None = None,
        state: str | None = None,
        resolution_method: str | None = None,
        resolution_confidence: int | None = None,
        resolution_raw: Any | None = None,
        risk_flags: Iterable[str] | None = None,
        primary_item_id: int | None = None,
        first_seen_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> IntelEvent:
        """Create or refresh one event inside one private daily build.

        Event rows never act as cross-edition history.  Repeated items from a
        prior date are represented by a fresh row in the active build and are
        matched only against final report entries by Stage C/D.
        """

        build_id = self._daily_build_id(run_id)
        key = _text(event_key)
        if key is None:
            raise ValueError("daily event requires event_key")
        canonical = _canonical_url(canonical_url)
        external = _normalize_event_external_id(external_id)
        norm_title = _normalize_event_title(normalized_title or title)
        aliases = _unique_strings(
            [
                *(value for value in (identity_keys or ()) if not str(value).strip().casefold().startswith("title:")),
                _identity_alias_url(canonical),
                _identity_alias_external(external),
                key if key.startswith(("url:", "external:")) else None,
            ]
        )

        row = self.session.scalar(
            select(IntelEvent).where(
                IntelEvent.build_id == build_id,
                IntelEvent.event_key == key,
            )
        )
        if row is None and primary_item_id is not None:
            row = self.session.scalar(
                select(IntelEvent)
                .join(IntelEventItem, IntelEventItem.event_id == IntelEvent.id)
                .where(
                    IntelEvent.build_id == build_id,
                    IntelEventItem.item_id == int(primary_item_id),
                )
                .order_by(IntelEvent.id.asc())
            )
        if row is None and canonical:
            row = self.session.scalar(
                select(IntelEvent).where(
                    IntelEvent.build_id == build_id,
                    IntelEvent.canonical_url == canonical,
                )
            )
        if row is None and external:
            row = self.session.scalar(
                select(IntelEvent).where(
                    IntelEvent.build_id == build_id,
                    IntelEvent.external_id == external,
                )
            )
        if row is None and aliases:
            alias_set = {alias for alias in aliases if alias.startswith(("url:", "external:"))}
            for candidate in self.session.scalars(
                select(IntelEvent)
                .where(IntelEvent.build_id == build_id)
                .order_by(IntelEvent.id.asc())
            ).all():
                existing_aliases = {
                    alias
                    for alias in _load_json(candidate.identity_keys_json, [])
                    if isinstance(alias, str) and alias.startswith(("url:", "external:"))
                }
                if existing_aliases & alias_set:
                    row = candidate
                    break
        if row is None:
            row = IntelEvent(event_key=key, build_id=build_id)
            self.session.add(row)

        def _assign(name: str, value: Any, *, allow_empty: bool = False) -> None:
            if value is not None and (allow_empty or value != ""):
                setattr(row, name, value)

        merged_topics = _unique_strings([*_string_values(_load_json(row.topics_json, [])), *(topics or ()), topic])
        merged_keywords = _unique_strings([*_string_values(_load_json(row.keywords_json, [])), *(keywords or ())])
        incoming_entities = [
            _structured_json(entity)
            for entity in (entities or ())
            if isinstance(_structured_json(entity), Mapping)
        ]
        merged_entities = _unique_json_objects([*_load_json(row.entities_json, []), *incoming_entities])
        merged_sources = _unique_strings([*_string_values(_load_json(row.source_ids_json, [])), *(source_ids or ())])
        merged_source_groups = _unique_strings(
            [*_string_values(_load_json(row.source_groups_json, [])), *(source_groups or ()), source_group]
        )
        previous_aliases = [
            alias
            for alias in _load_json(row.identity_keys_json, [])
            if isinstance(alias, str) and alias.startswith(("url:", "external:"))
        ]
        merged_aliases = _unique_strings([*previous_aliases, *aliases])

        _assign("canonical_url", canonical)
        _assign("external_id", external)
        _assign("normalized_title", norm_title, allow_empty=True)
        _assign("title", _text(title))
        _assign("summary_cn", _text(summary_cn))
        _assign("topic", _text(topic))
        _assign("content_class", _text(content_class))
        _assign("source_group", _text(source_group))
        if merged_topics:
            row.topics_json = _dump_json(merged_topics)
            if not row.topic or row.topic == "technology_insight":
                row.topic = merged_topics[0]
        if merged_keywords:
            row.keywords_json = _dump_json(merged_keywords)
        if merged_entities:
            row.entities_json = _dump_json(merged_entities)
        if merged_sources:
            row.source_ids_json = _dump_json(merged_sources)
        if merged_source_groups:
            row.source_groups_json = _dump_json(merged_source_groups)
        if merged_aliases:
            row.identity_keys_json = _dump_json(merged_aliases)
        if display_score is not None:
            try:
                row.display_score = max(float(row.display_score or 0.0), max(0.0, min(100.0, float(display_score))))
            except (TypeError, ValueError, OverflowError):
                pass
        novelty = _normalize_novelty_status(novelty_status)
        if novelty is not None and (row.novelty_status in {None, "unknown"} or novelty != "unknown"):
            row.novelty_status = novelty
        _assign("state", _text(state))
        _assign("resolution_method", _text(resolution_method))
        if resolution_confidence is not None:
            try:
                row.resolution_confidence = max(0, min(100, int(resolution_confidence)))
            except (TypeError, ValueError, OverflowError):
                row.resolution_confidence = 0
        if resolution_raw is not None:
            row.resolution_raw_json = _dump_json(resolution_raw)
        row.risk_flags_json = _dump_json(
            _unique_strings([*_string_values(_load_json(row.risk_flags_json, [])), *(risk_flags or ())])
        )
        if primary_item_id is not None and row.primary_item_id is None:
            row.primary_item_id = int(primary_item_id)
        first_seen = _as_utc(first_seen_at)
        last_seen = _as_utc(last_seen_at)
        existing_first_seen = _as_utc(row.first_seen_at)
        existing_last_seen = _as_utc(row.last_seen_at)
        if first_seen is not None and (existing_first_seen is None or first_seen < existing_first_seen):
            row.first_seen_at = first_seen
        if last_seen is not None and (existing_last_seen is None or last_seen > existing_last_seen):
            row.last_seen_at = last_seen
        if metadata:
            existing_metadata = _load_json(row.resolution_raw_json, {})
            if isinstance(existing_metadata, Mapping):
                row.resolution_raw_json = _dump_json({**existing_metadata, **dict(metadata)})
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def get_event(self, event_id: int) -> IntelEvent | None:
        return self.session.get(IntelEvent, event_id)

    def find_event_for_item(self, item_id: int) -> IntelEvent | None:
        return self.session.scalar(
            select(IntelEvent)
            .join(IntelEventItem, IntelEventItem.event_id == IntelEvent.id)
            .where(IntelEventItem.item_id == int(item_id))
            .order_by(IntelEvent.id.asc())
        )

    def upsert_event_item(
        self,
        event_id: int,
        item_id: int,
        *,
        source_id: str | None = None,
        source_group: str | None = None,
        identity_key: str | None = None,
        match_type: str = "deterministic",
        match_confidence: int = 100,
        is_primary: bool = False,
        lineage: Mapping[str, Any] | None = None,
    ) -> EventItemUpsertResult:
        """Attach one member item to an event idempotently.

        An item belongs to at most one event.  If a rerun discovers a stronger
        identity, the existing relation is moved to the selected event rather
        than creating a second membership row.
        """

        item = self.session.get(IntelItem, int(item_id))
        if item is None:
            raise ValueError(f"intel item {item_id} does not exist")
        relation = self.session.scalar(
            select(IntelEventItem).where(
                IntelEventItem.event_id == int(event_id),
                IntelEventItem.item_id == int(item_id),
            )
        )
        created = relation is None
        if relation is None:
            relation = self.session.scalar(select(IntelEventItem).where(IntelEventItem.item_id == int(item_id)))
        if relation is None:
            relation = IntelEventItem(event_id=int(event_id), item_id=int(item_id), source_id=source_id or item.source_id)
            self.session.add(relation)
        else:
            relation.event_id = int(event_id)
            relation.source_id = source_id or relation.source_id or item.source_id
        relation.source_group = source_group or relation.source_group or (item.source.source_group if item.source else None)
        relation.source_url = relation.source_url or item.canonical_url or item.source_url
        relation.source_title = relation.source_title or item.title
        relation.identity_key = identity_key or relation.identity_key
        relation.match_type = _text(match_type) or "deterministic"
        try:
            relation.match_confidence = max(0, min(100, int(match_confidence)))
        except (TypeError, ValueError, OverflowError):
            relation.match_confidence = 0
        relation.is_primary = bool(is_primary)
        if lineage is not None:
            relation.lineage_json = _dump_json(dict(lineage))
        relation.updated_at = utcnow()
        self.session.flush()
        return EventItemUpsertResult(relation, created)

    def list_event_items(self, event_id: int) -> list[IntelEventItem]:
        stmt = (
            select(IntelEventItem)
            .where(IntelEventItem.event_id == int(event_id))
            .options(joinedload(IntelEventItem.item).joinedload(IntelItem.source))
            .order_by(IntelEventItem.is_primary.desc(), IntelEventItem.id.asc())
        )
        return list(self.session.scalars(stmt).unique().all())

    def upsert_event_stage_d_snapshot(
        self,
        event_id: int,
        *,
        run_id: int,
        display_order: int = 0,
        display_score: float = 0.0,
        selected: bool = False,
        topic: str | None = None,
        source_group: str | None = None,
        content_class: str | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EventStageDSnapshotUpsertResult:
        row = self.session.scalar(
            select(IntelEventStageDSnapshot).where(
                IntelEventStageDSnapshot.run_id == int(run_id),
                IntelEventStageDSnapshot.event_id == int(event_id),
            )
        )
        created = row is None
        if row is None:
            row = IntelEventStageDSnapshot(event_id=int(event_id), run_id=int(run_id))
            self.session.add(row)
        row.run_id = int(run_id)
        row.display_order = max(0, int(display_order))
        try:
            row.display_score = max(0.0, min(100.0, float(display_score)))
        except (TypeError, ValueError, OverflowError):
            row.display_score = 0.0
        row.selected = bool(selected)
        row.topic = _text(topic)
        row.source_group = _text(source_group)
        row.content_class = _text(content_class)
        row.reason = _text(reason)
        row.metadata_json = _dump_json(metadata or {})
        row.updated_at = utcnow()
        self.session.flush()
        return EventStageDSnapshotUpsertResult(row, created)

    def clear_event_stage_d_snapshot(self, *, run_id: int) -> int:
        """Remove stale Stage-D rows for one private daily build."""

        rows = list(
            self.session.scalars(
                select(IntelEventStageDSnapshot).where(
                    IntelEventStageDSnapshot.run_id == int(run_id),
                )
            ).all()
        )
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)

    # ------------------------------------------------------------------
    # Durable resumable run/stage/task/attempt state
    # ------------------------------------------------------------------

    def ensure_stage(
        self,
        run_id: int,
        stage_name: str,
        *,
        status: str = "pending",
        input_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        reference_time: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> IntelRunStage:
        """Create or idempotently refresh one run-stage row.

        Fingerprint changes invalidate previously successful work but retain
        all immutable attempts for audit.  ``force`` is deliberately scoped to
        this stage and never cascades to another stage.
        """

        run = self.session.get(IntelRun, int(run_id))
        if run is None:
            raise ValueError(f"intel run {run_id} does not exist")
        name = _text(stage_name)
        if not name:
            raise ValueError("stage_name is required")
        row = self.session.scalar(
            select(IntelRunStage).where(
                IntelRunStage.run_id == int(run_id), IntelRunStage.stage_name == name
            )
        )
        created = row is None
        if row is None:
            row = IntelRunStage(run_id=int(run_id), stage_name=name, status=status or "pending")
            self.session.add(row)
        old_input = row.input_fingerprint
        old_config = row.config_fingerprint
        if input_fingerprint is not None:
            row.input_fingerprint = _text(input_fingerprint)
        if config_fingerprint is not None:
            row.config_fingerprint = _text(config_fingerprint)
        run_reference = run.reference_time or _as_utc(run.started_at)
        requested_reference = _as_utc(reference_time)
        if run.reference_time is not None and requested_reference is not None and requested_reference != run_reference:
            raise ValueError(
                f"intel run {run_id} reference_time is frozen at {run_reference.isoformat()}"
            )
        if run.reference_time is None and requested_reference is not None:
            run.reference_time = requested_reference
            run_reference = requested_reference
        if row.reference_time is None:
            row.reference_time = run_reference
        if metadata is not None:
            existing = _load_json(row.metadata_json, {})
            row.metadata_json = _dump_json(
                {**(dict(existing) if isinstance(existing, Mapping) else {}), **dict(metadata)}
            )
        fingerprint_changed = (
            not created
            and ((input_fingerprint is not None and old_input != row.input_fingerprint)
                 or (config_fingerprint is not None and old_config != row.config_fingerprint))
        )
        if fingerprint_changed or force:
            self._reset_stage_tasks(row.id, include_succeeded=force or fingerprint_changed)
            row.status = status or "pending"
            row.finished_at = None
            row.next_retry_at = None
            row.error_category = None
            row.error_code = None
            row.error_message = None
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def get_stage(self, run_id: int, stage_name: str) -> IntelRunStage | None:
        return self.session.scalar(
            select(IntelRunStage).where(
                IntelRunStage.run_id == int(run_id), IntelRunStage.stage_name == str(stage_name)
            )
        )

    def get_stage_by_id(self, stage_id: int) -> IntelRunStage | None:
        return self.session.get(IntelRunStage, int(stage_id))

    def list_stages(self, run_id: int, *, statuses: Iterable[str] | None = None) -> list[IntelRunStage]:
        stmt = select(IntelRunStage).where(IntelRunStage.run_id == int(run_id)).order_by(IntelRunStage.id.asc())
        if statuses:
            stmt = stmt.where(IntelRunStage.status.in_(list(statuses)))
        return list(self.session.scalars(stmt).all())

    def start_stage(
        self,
        run_id: int,
        stage_name: str,
        *,
        owner: str | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
        **kwargs: Any,
    ) -> IntelRunStage | None:
        """Start a stage and acquire its single-stage execution lease."""

        stage_row = self.ensure_stage(run_id, stage_name, **kwargs)
        return self.acquire_stage_lease(
            stage_row,
            owner=owner or lease_owner,
            lease_seconds=lease_seconds,
            now=now,
        )

    def acquire_stage_lease(
        self,
        stage: IntelRunStage | int,
        *,
        owner: str | None = None,
        lease_owner: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> IntelRunStage | None:
        """Acquire/renew a stage lease; return ``None`` on ownership conflict."""

        row = self._coerce_stage(stage)
        if row is None:
            return None
        current = _as_utc(now) or utcnow()
        owner_value = _text(owner or lease_owner or worker_id) or f"worker-{uuid4().hex}"
        active = bool(row.lease_owner and row.lease_expires_at and _as_utc(row.lease_expires_at) > current)
        if active and row.lease_owner != owner_value:
            return None
        prior_status = row.status
        lease_was_expired = bool(
            row.lease_expires_at is not None and _as_utc(row.lease_expires_at) <= current
        )
        row.lease_owner = owner_value
        row.lease_expires_at = current + timedelta(seconds=max(1, int(lease_seconds)))
        row.heartbeat_at = current
        if row.status not in STAGE_RUNNING_STATUSES or lease_was_expired:
            row.status = "running"
            row.started_at = row.started_at or current
            row.attempt_count = int(row.attempt_count or 0) + 1
            if prior_status in {"succeeded", "failed", "blocked", "cancelled", "skipped"}:
                row.finished_at = None
        row.updated_at = current
        self.session.flush()
        return row

    def heartbeat_stage(
        self,
        stage: IntelRunStage | int,
        *,
        owner: str | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> IntelRunStage | None:
        row = self._coerce_stage(stage)
        if row is None:
            return None
        current = _as_utc(now) or utcnow()
        expected_owner = _text(owner or lease_owner)
        if expected_owner and row.lease_owner != expected_owner:
            return None
        if row.lease_expires_at is not None and _as_utc(row.lease_expires_at) <= current:
            return None
        row.heartbeat_at = current
        row.lease_expires_at = current + timedelta(seconds=max(1, int(lease_seconds)))
        row.updated_at = current
        self.session.flush()
        return row

    def release_stage_lease(
        self,
        stage: IntelRunStage | int,
        *,
        owner: str | None = None,
        lease_owner: str | None = None,
        status: str | None = None,
    ) -> IntelRunStage | None:
        row = self._coerce_stage(stage)
        if row is None:
            return None
        expected_owner = _text(owner or lease_owner)
        if expected_owner and row.lease_owner not in {None, expected_owner}:
            return None
        row.lease_owner = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        if status:
            row.status = _text(status) or row.status
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def finish_stage(
        self,
        stage: IntelRunStage | int,
        *,
        status: str = "succeeded",
        result_ref: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
        error_category: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        owner: str | None = None,
        lease_owner: str | None = None,
    ) -> IntelRunStage | None:
        row = self._coerce_stage(stage)
        if row is None:
            return None
        expected_owner = _text(owner or lease_owner)
        if expected_owner and row.lease_owner not in {None, expected_owner}:
            return None
        row.status = _text(status) or "succeeded"
        row.finished_at = utcnow()
        row.lease_owner = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        if result_ref is not None:
            row.result_ref_json = _dump_json(_structured_json(result_ref))
        if metadata is not None:
            row.metadata_json = _dump_json(dict(metadata))
        row.error_category = _text(error_category)
        row.error_code = _text(error_code)
        row.error_message = _text(error_message)
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def ensure_stage_task(
        self,
        stage: IntelRunStage | int | None = None,
        *,
        stage_id: int | None = None,
        subject_type: str = "item",
        subject_id: Any | None = None,
        subject: Any | None = None,
        subject_key: Any | None = None,
        input_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        item_id: int | None = None,
        event_id: int | None = None,
        target_run_id: int | None = None,
        status: str = "pending",
        result_ref: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> IntelRunStageTask:
        stage_row = self._coerce_stage(stage if stage is not None else stage_id)
        if stage_row is None:
            raise ValueError(f"intel run stage {stage!r} does not exist")
        kind = _normalize_subject_type(subject_type)
        subject_value = _text(subject_id if subject_id is not None else (subject if subject is not None else subject_key))
        if not subject_value:
            if kind == "item" and item_id is not None:
                subject_value = str(int(item_id))
            elif kind == "event" and event_id is not None:
                subject_value = str(int(event_id))
            elif kind == "run" and target_run_id is not None:
                subject_value = str(int(target_run_id))
        if not subject_value:
            raise ValueError("subject_id is required")
        resolved_item_id = item_id
        if kind == "item" and resolved_item_id is None and subject_value.isdigit():
            resolved_item_id = int(subject_value)
        if kind == "item" and resolved_item_id is not None:
            item = self.session.get(IntelItem, int(resolved_item_id))
            if item is not None:
                in_scope = self.session.scalar(
                    select(IntelRunItem.id).where(
                        IntelRunItem.run_id == stage_row.run_id,
                        IntelRunItem.item_id == int(resolved_item_id),
                    )
                )
                if in_scope is None:
                    raise ValueError(
                        f"item {resolved_item_id} is not attached to intel run {stage_row.run_id}"
                    )
        task = self.session.scalar(
            select(IntelRunStageTask).where(
                IntelRunStageTask.stage_id == stage_row.id,
                IntelRunStageTask.subject_type == kind,
                IntelRunStageTask.subject_id == subject_value,
            )
        )
        created = task is None
        if task is None:
            task = IntelRunStageTask(
                stage_id=stage_row.id,
                subject_type=kind,
                subject_id=subject_value,
                status=status or "pending",
            )
            self.session.add(task)
        changed = (
            (input_fingerprint is not None and task.input_fingerprint not in {None, input_fingerprint})
            or (config_fingerprint is not None and task.config_fingerprint not in {None, config_fingerprint})
        )
        if input_fingerprint is not None:
            task.input_fingerprint = _text(input_fingerprint)
        if config_fingerprint is not None:
            task.config_fingerprint = _text(config_fingerprint)
        task.item_id = resolved_item_id if resolved_item_id is not None else task.item_id
        task.event_id = event_id if event_id is not None else task.event_id
        task.target_run_id = target_run_id if target_run_id is not None else task.target_run_id
        if kind == "item" and task.item_id is None and subject_value.isdigit():
            task.item_id = int(subject_value)
        elif kind == "event" and task.event_id is None and subject_value.isdigit():
            task.event_id = int(subject_value)
        elif kind == "run" and task.target_run_id is None and subject_value.isdigit():
            task.target_run_id = int(subject_value)
        if changed or force:
            self._reset_task(task, include_succeeded=True)
            task.status = status or "pending"
        if result_ref is not None:
            task.result_ref_json = _dump_json(_structured_json(result_ref))
        if metadata is not None:
            # Task metadata is represented by result JSON for the compact
            # schema; callers still get a stable JSON reference.
            current = _load_json(task.result_json, {})
            task.result_json = _dump_json(
                {**(dict(current) if isinstance(current, Mapping) else {}), "metadata": dict(metadata)}
            )
        if created:
            task.updated_at = utcnow()
        self.session.flush()
        return task

    def get_stage_task(self, task_id: int | IntelRunStageTask) -> IntelRunStageTask | None:
        if isinstance(task_id, IntelRunStageTask):
            return task_id
        try:
            return self.session.get(IntelRunStageTask, int(task_id))
        except (TypeError, ValueError):
            return None

    def get_task(
        self,
        stage: IntelRunStage | int,
        *,
        subject_type: str = "item",
        subject_id: Any,
    ) -> IntelRunStageTask | None:
        stage_row = self._coerce_stage(stage)
        if stage_row is None:
            return None
        return self.session.scalar(
            select(IntelRunStageTask).where(
                IntelRunStageTask.stage_id == stage_row.id,
                IntelRunStageTask.subject_type == _normalize_subject_type(subject_type),
                IntelRunStageTask.subject_id == str(subject_id),
            )
        )

    def list_stage_tasks(
        self,
        stage: IntelRunStage | int | None = None,
        *,
        stage_id: int | None = None,
        statuses: Iterable[str] | None = None,
        subject_type: str | None = None,
        input_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        reusable_only: bool = False,
        include_expired: bool = False,
        limit: int | None = None,
    ) -> list[IntelRunStageTask]:
        stage_row = self._coerce_stage(stage if stage is not None else stage_id)
        if stage_row is None:
            return []
        stmt = select(IntelRunStageTask).where(IntelRunStageTask.stage_id == stage_row.id).order_by(IntelRunStageTask.id.asc())
        if statuses:
            stmt = stmt.where(IntelRunStageTask.status.in_(list(statuses)))
        if subject_type:
            stmt = stmt.where(IntelRunStageTask.subject_type == _normalize_subject_type(subject_type))
        if input_fingerprint is not None:
            stmt = stmt.where(IntelRunStageTask.input_fingerprint == str(input_fingerprint))
        if config_fingerprint is not None:
            stmt = stmt.where(IntelRunStageTask.config_fingerprint == str(config_fingerprint))
        if reusable_only:
            stmt = stmt.where(IntelRunStageTask.status == TASK_REUSABLE_STATUS)
        if not include_expired:
            # Expired running tasks are intentionally hidden from normal query
            # callers; ``recover_expired_stage_tasks`` exposes them explicitly.
            stmt = stmt.where(
                or_(
                    IntelRunStageTask.status != "running",
                    IntelRunStageTask.lease_expires_at.is_(None),
                    IntelRunStageTask.lease_expires_at > utcnow(),
                )
            )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def task_is_reusable(
        self,
        task: IntelRunStageTask | int,
        *,
        input_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
    ) -> bool:
        row = self.get_stage_task(task) if isinstance(task, (int, str)) else task
        if row is None or row.status != TASK_REUSABLE_STATUS:
            return False
        if input_fingerprint is not None and row.input_fingerprint != str(input_fingerprint):
            return False
        if config_fingerprint is not None and row.config_fingerprint != str(config_fingerprint):
            return False
        return True

    def claim_stage_task(
        self,
        stage: IntelRunStage | int | None = None,
        *,
        stage_id: int | None = None,
        task_id: int | None = None,
        subject_type: str = "item",
        subject_id: Any | None = None,
        owner: str | None = None,
        lease_owner: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
        input_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        force: bool = False,
        acquire_stage: bool = True,
    ) -> IntelRunStageTask | None:
        """Atomically claim one pending/retryable task and create an attempt."""

        stage_row = self._coerce_stage(stage if stage is not None else stage_id)
        if stage_row is None:
            return None
        current = _as_utc(now) or utcnow()
        owner_value = _text(owner or lease_owner or worker_id) or f"worker-{uuid4().hex}"
        if task_id is not None:
            task = self.session.get(IntelRunStageTask, int(task_id))
            if task is None or task.stage_id != stage_row.id:
                return None
        else:
            if subject_id is None:
                return None
            task = self.get_task(stage_row, subject_type=subject_type, subject_id=subject_id)
            if task is None:
                return None

        # A successful projection is reusable only under the same input and
        # stage-contract fingerprints.  A changed fingerprint (or explicit
        # stage-scoped force) resets this task while retaining prior attempts.
        reusable = self.task_is_reusable(
            task, input_fingerprint=input_fingerprint, config_fingerprint=config_fingerprint
        )
        if task.status == "succeeded" and not force and reusable:
            return None
        if force and task.status in {"succeeded", "blocked", "cancelled", "skipped"}:
            self._reset_task(task, include_succeeded=True)
        elif task.status == "succeeded" and not reusable:
            self._reset_task(task, include_succeeded=True)
        expired = task.status == "running" and (
            task.lease_expires_at is None or _as_utc(task.lease_expires_at) <= current
        )
        if task.status == "running" and not expired:
            return None
        if expired:
            previous_attempt = self._current_attempt(task)
            if previous_attempt is not None and previous_attempt.status == "running":
                self._finish_attempt_row(
                    previous_attempt,
                    status="retry_waiting",
                    finished_at=current,
                    retryable=True,
                    next_retry_at=current,
                    error_category="lease_expired",
                    error_code="lease_expired",
                    error_message="task lease expired before completion",
                )
        due = task.next_retry_at is None or _as_utc(task.next_retry_at) <= current
        eligible = task.status in {"pending", "retry_waiting", "failed"} and due
        if not eligible and not expired:
            return None
        if acquire_stage and self.acquire_stage_lease(
            stage_row, owner=owner_value, lease_seconds=lease_seconds, now=current
        ) is None:
            return None
        task.status = "running"
        task.lease_owner = owner_value
        task.lease_expires_at = current + timedelta(seconds=max(1, int(lease_seconds)))
        task.heartbeat_at = current
        task.next_retry_at = None
        task.attempt_count = int(task.attempt_count or 0) + 1
        if input_fingerprint is not None:
            task.input_fingerprint = str(input_fingerprint)
        if config_fingerprint is not None:
            task.config_fingerprint = str(config_fingerprint)
        task.error_category = None
        task.error_code = None
        task.error_message = None
        task.updated_at = current
        self.session.flush()
        attempt = IntelRunStageAttempt(
            task_id=task.id,
            attempt_no=task.attempt_count,
            status="running",
            started_at=current,
            lease_owner=owner_value,
            lease_expires_at=task.lease_expires_at,
            heartbeat_at=current,
            input_fingerprint=task.input_fingerprint,
            config_fingerprint=task.config_fingerprint,
        )
        self.session.add(attempt)
        self.session.flush()
        task.last_attempt_id = attempt.id
        self.session.flush()
        return task

    def claim_stage_tasks(
        self,
        stage: IntelRunStage | int | None = None,
        *,
        stage_id: int | None = None,
        limit: int = 1,
        owner: str | None = None,
        lease_owner: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
        statuses: Iterable[str] | None = None,
        force: bool = False,
        input_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
    ) -> list[IntelRunStageTask]:
        stage_row = self._coerce_stage(stage if stage is not None else stage_id)
        if stage_row is None or int(limit) <= 0:
            return []
        current = _as_utc(now) or utcnow()
        owner_value = _text(owner or lease_owner or worker_id) or f"worker-{uuid4().hex}"
        if self.acquire_stage_lease(stage_row, owner=owner_value, lease_seconds=lease_seconds, now=current) is None:
            return []
        allowed = list(statuses) if statuses else ["pending", "retry_waiting", "failed", "running"]
        stmt = select(IntelRunStageTask).where(
            IntelRunStageTask.stage_id == stage_row.id,
            IntelRunStageTask.status.in_(allowed),
        ).order_by(IntelRunStageTask.id.asc())
        candidates = list(self.session.scalars(stmt).all())
        claimed: list[IntelRunStageTask] = []
        for task in candidates:
            if len(claimed) >= int(limit):
                break
            row = self.claim_stage_task(
                stage_row,
                task_id=task.id,
                owner=owner_value,
                lease_seconds=lease_seconds,
                now=current,
                force=force,
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
                acquire_stage=False,
            )
            if row is not None:
                claimed.append(row)
        return claimed

    def heartbeat_stage_task(
        self,
        task: IntelRunStageTask | int,
        *,
        owner: str | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> IntelRunStageTask | None:
        row = self.get_stage_task(task) if isinstance(task, (int, str)) else task
        if row is None:
            return None
        current = _as_utc(now) or utcnow()
        expected_owner = _text(owner or lease_owner)
        if row.status != "running" or (expected_owner and row.lease_owner != expected_owner):
            return None
        if row.lease_expires_at is not None and _as_utc(row.lease_expires_at) <= current:
            return None
        row.heartbeat_at = current
        row.lease_expires_at = current + timedelta(seconds=max(1, int(lease_seconds)))
        attempt = self._current_attempt(row)
        if attempt is not None:
            attempt.heartbeat_at = current
            attempt.lease_expires_at = row.lease_expires_at
        row.updated_at = current
        self.session.flush()
        return row

    def complete_stage_task(
        self,
        task: IntelRunStageTask | int,
        *,
        result_ref: Any | None = None,
        result: Any | None = None,
        raw_response: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
        owner: str | None = None,
        lease_owner: str | None = None,
        status: str = "succeeded",
        now: datetime | None = None,
    ) -> IntelRunStageTask | None:
        row = self.get_stage_task(task) if isinstance(task, (int, str)) else task
        if row is None:
            return None
        expected_owner = _text(owner or lease_owner)
        if expected_owner and row.lease_owner not in {None, expected_owner}:
            return None
        current = _as_utc(now) or utcnow()
        if (
            row.status == "running"
            and row.lease_expires_at is not None
            and _as_utc(row.lease_expires_at) <= current
        ):
            return None
        attempt = self._current_attempt(row)
        if attempt is not None:
            self._finish_attempt_row(
                attempt,
                status=_text(status) or "succeeded",
                finished_at=current,
                result_ref=result_ref,
                raw_response=raw_response,
                metadata=metadata,
            )
        row.status = _text(status) or "succeeded"
        if result_ref is not None:
            row.result_ref_json = _dump_json(_structured_json(result_ref))
        if result is not None:
            row.result_json = _dump_json(_structured_json(result))
        elif metadata is not None:
            current_result = _load_json(row.result_json, {})
            row.result_json = _dump_json(
                {**(dict(current_result) if isinstance(current_result, Mapping) else {}), "metadata": dict(metadata)}
            )
        row.lease_owner = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.next_retry_at = None
        row.error_category = None
        row.error_code = None
        row.error_message = None
        row.updated_at = current
        self.session.flush()
        self.refresh_stage_status(row.stage_id, now=current)
        return row

    def fail_stage_task(
        self,
        task: IntelRunStageTask | int,
        *,
        error_category: str | None = None,
        category: str | None = None,
        error_code: str | None = None,
        code: str | None = None,
        error_message: str | None = None,
        message: str | None = None,
        retryable: bool | None = None,
        retry_after_seconds: int | float | None = None,
        next_retry_at: datetime | None = None,
        raw_response: Any | None = None,
        owner: str | None = None,
        lease_owner: str | None = None,
        now: datetime | None = None,
    ) -> IntelRunStageTask | None:
        row = self.get_stage_task(task) if isinstance(task, (int, str)) else task
        if row is None:
            return None
        expected_owner = _text(owner or lease_owner)
        if expected_owner and row.lease_owner not in {None, expected_owner}:
            return None
        current = _as_utc(now) or utcnow()
        if (
            row.status == "running"
            and row.lease_expires_at is not None
            and _as_utc(row.lease_expires_at) <= current
        ):
            return None
        category_value = _text(error_category or category) or "provider"
        code_value = _text(error_code or code)
        message_value = _text(error_message or message)
        if retryable is None:
            retryable = _retryable_stage_error(category_value, code_value, message_value)
        due = _as_utc(next_retry_at)
        if due is None and retryable:
            delay = max(0.0, float(retry_after_seconds or 0.0))
            due = current + timedelta(seconds=delay) if delay else current
        attempt = self._current_attempt(row)
        if attempt is not None and attempt.status == "running":
            self._finish_attempt_row(
                attempt,
                status="retry_waiting" if retryable else "blocked",
                finished_at=current,
                retryable=bool(retryable),
                next_retry_at=due,
                error_category=category_value,
                error_code=code_value,
                error_message=message_value,
                raw_response=raw_response,
            )
        row.status = "retry_waiting" if retryable else "blocked"
        row.next_retry_at = due
        row.lease_owner = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.error_category = category_value
        row.error_code = code_value
        row.error_message = message_value[:4000] if message_value else None
        row.updated_at = current
        self.session.flush()
        self.refresh_stage_status(row.stage_id, now=current)
        return row

    def start_stage_attempt(
        self,
        task: IntelRunStageTask | int,
        *,
        owner: str | None = None,
        lease_owner: str | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> IntelRunStageAttempt | None:
        row = self.get_stage_task(task) if isinstance(task, (int, str)) else task
        if row is None:
            return None
        current = _as_utc(now) or utcnow()
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.status = "running"
        row.lease_owner = _text(owner or lease_owner)
        row.lease_expires_at = current + timedelta(seconds=max(1, int(lease_seconds)))
        row.heartbeat_at = current
        attempt = IntelRunStageAttempt(
            task_id=row.id,
            attempt_no=row.attempt_count,
            status="running",
            started_at=current,
            lease_owner=row.lease_owner,
            lease_expires_at=row.lease_expires_at,
            heartbeat_at=current,
            input_fingerprint=row.input_fingerprint,
            config_fingerprint=row.config_fingerprint,
        )
        self.session.add(attempt)
        self.session.flush()
        row.last_attempt_id = attempt.id
        self.session.flush()
        return attempt

    def finish_stage_attempt(
        self,
        attempt: IntelRunStageAttempt | int,
        *,
        status: str = "succeeded",
        result_ref: Any | None = None,
        raw_response: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
        error_category: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        next_retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> IntelRunStageAttempt | None:
        row = self.session.get(IntelRunStageAttempt, int(attempt)) if isinstance(attempt, (int, str)) else attempt
        if row is None:
            return None
        self._finish_attempt_row(
            row,
            status=_text(status) or "succeeded",
            finished_at=_as_utc(now) or utcnow(),
            result_ref=result_ref,
            raw_response=raw_response,
            metadata=metadata,
            error_category=error_category,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            next_retry_at=_as_utc(next_retry_at),
        )
        self.session.flush()
        return row

    def list_stage_attempts(self, task: IntelRunStageTask | int, *, limit: int | None = None) -> list[IntelRunStageAttempt]:
        task_id = int(task.id if isinstance(task, IntelRunStageTask) else task)
        stmt = select(IntelRunStageAttempt).where(IntelRunStageAttempt.task_id == task_id).order_by(IntelRunStageAttempt.attempt_no.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def recover_expired_stage_tasks(
        self,
        stage: IntelRunStage | int | None = None,
        *,
        now: datetime | None = None,
    ) -> list[IntelRunStageTask]:
        """Make expired leases retryable without deleting their audit rows."""

        current = _as_utc(now) or utcnow()
        stage_id = self._stage_id(stage) if stage is not None else None
        stmt = select(IntelRunStageTask).where(
            IntelRunStageTask.status == "running",
            IntelRunStageTask.lease_expires_at.is_not(None),
            IntelRunStageTask.lease_expires_at <= current,
        )
        if stage_id is not None:
            stmt = stmt.where(IntelRunStageTask.stage_id == stage_id)
        rows = list(self.session.scalars(stmt).all())
        for row in rows:
            attempt = self._current_attempt(row)
            if attempt is not None and attempt.status == "running":
                self._finish_attempt_row(
                    attempt,
                    status="retry_waiting",
                    finished_at=current,
                    retryable=True,
                    next_retry_at=current,
                    error_category="lease_expired",
                    error_code="lease_expired",
                    error_message="task lease expired before completion",
                )
            row.status = "retry_waiting"
            row.next_retry_at = current
            row.lease_owner = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            row.error_category = "lease_expired"
            row.error_code = "lease_expired"
            row.error_message = "task lease expired before completion"
            row.updated_at = current
        if rows:
            self.session.flush()
            touched_stage_ids = {row.stage_id for row in rows}
            for touched_stage_id in touched_stage_ids:
                self.refresh_stage_status(touched_stage_id, now=current)
        return rows

    def retry_failed(
        self,
        stage: IntelRunStage | int,
        *,
        include_blocked: bool = False,
        task_ids: Iterable[int] | None = None,
        now: datetime | None = None,
        reset_attempt_count: bool = False,
    ) -> list[IntelRunStageTask]:
        stage_row = self._coerce_stage(stage)
        if stage_row is None:
            return []
        statuses = {"failed", "retry_waiting"}
        if include_blocked:
            statuses.add("blocked")
        requested = {int(value) for value in task_ids} if task_ids is not None else None
        rows = list(
            self.session.scalars(
                select(IntelRunStageTask).where(
                    IntelRunStageTask.stage_id == stage_row.id,
                    IntelRunStageTask.status.in_(statuses),
                ).order_by(IntelRunStageTask.id.asc())
            ).all()
        )
        current = _as_utc(now) or utcnow()
        selected: list[IntelRunStageTask] = []
        for row in rows:
            if requested is not None and row.id not in requested:
                continue
            row.status = "pending"
            row.next_retry_at = None
            row.lease_owner = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            row.error_category = None
            row.error_code = None
            row.error_message = None
            if reset_attempt_count:
                row.attempt_count = 0
            row.updated_at = current
            selected.append(row)
        if selected:
            stage_row.status = "pending"
            stage_row.finished_at = None
            stage_row.next_retry_at = None
            stage_row.error_category = None
            stage_row.error_code = None
            stage_row.error_message = None
            stage_row.lease_owner = None
            stage_row.lease_expires_at = None
            stage_row.heartbeat_at = None
            stage_row.updated_at = current
            self.session.flush()
            self.refresh_stage_status(stage_row, now=current)
        return selected

    def reset_stage(
        self,
        stage: IntelRunStage | int,
        *,
        include_succeeded: bool = False,
        reset_attempt_count: bool = False,
    ) -> IntelRunStage | None:
        row = self._coerce_stage(stage)
        if row is None:
            return None
        self._reset_stage_tasks(row.id, include_succeeded=include_succeeded, reset_attempt_count=reset_attempt_count)
        row.status = "pending"
        row.finished_at = None
        row.next_retry_at = None
        row.lease_owner = None
        row.lease_expires_at = None
        row.heartbeat_at = None
        row.error_category = None
        row.error_code = None
        row.error_message = None
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def stage_summary(self, stage: IntelRunStage | int) -> StageStateSummary | None:
        row = self._coerce_stage(stage)
        if row is None:
            return None
        tasks = list(self.session.scalars(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == row.id)).all())
        counts = {status: 0 for status in ("pending", "running", "succeeded", "failed", "retry_waiting", "blocked")}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return StageStateSummary(
            stage_id=row.id,
            run_id=row.run_id,
            stage_name=row.stage_name,
            status=row.status,
            total=len(tasks),
            pending=counts.get("pending", 0),
            running=counts.get("running", 0),
            succeeded=counts.get("succeeded", 0),
            failed=counts.get("failed", 0),
            retry_waiting=counts.get("retry_waiting", 0),
            blocked=counts.get("blocked", 0),
        )

    def refresh_stage_status(
        self,
        stage: IntelRunStage | int,
        *,
        now: datetime | None = None,
    ) -> IntelRunStage | None:
        """Derive stage status from durable task state without touching projections."""

        row = self._coerce_stage(stage)
        if row is None:
            return None
        tasks = list(
            self.session.scalars(
                select(IntelRunStageTask).where(IntelRunStageTask.stage_id == row.id)
            ).all()
        )
        if not tasks:
            return row
        current = _as_utc(now) or utcnow()
        statuses = {task.status for task in tasks}
        representative_error = next(
            (task for task in tasks if task.error_code or task.error_message), None
        )
        if statuses <= {"succeeded", "skipped", "cancelled"}:
            row.status = "succeeded"
            row.finished_at = row.finished_at or current
            row.lease_owner = None
            row.lease_expires_at = None
            row.heartbeat_at = None
        elif "running" in statuses:
            row.status = "running"
            row.finished_at = None
        elif "blocked" in statuses and not (statuses & {"pending", "retry_waiting", "failed"}):
            row.status = "blocked"
            row.finished_at = row.finished_at or current
        elif "failed" in statuses and not (statuses & {"pending", "retry_waiting"}):
            row.status = "failed"
            row.finished_at = row.finished_at or current
        else:
            row.status = "pending"
            row.finished_at = None
        if "running" not in statuses:
            row.lease_owner = None
            row.lease_expires_at = None
            row.heartbeat_at = None
        if representative_error is not None:
            row.error_category = representative_error.error_category
            row.error_code = representative_error.error_code
            row.error_message = representative_error.error_message
        elif row.status in {"succeeded", "pending", "running"}:
            row.error_category = None
            row.error_code = None
            row.error_message = None
        row.updated_at = current
        self.session.flush()
        return row


    def run_stage_summary(self, run_id: int) -> list[StageStateSummary]:
        return [summary for stage in self.list_stages(run_id) if (summary := self.stage_summary(stage)) is not None]

    def status_summary(self, run_id: int) -> dict[str, Any]:
        summaries = self.run_stage_summary(run_id)
        return {
            "run_id": int(run_id),
            "stages": [asdict(summary) for summary in summaries],
            "total_tasks": sum(summary.total for summary in summaries),
            "succeeded_tasks": sum(summary.succeeded for summary in summaries),
            "failed_tasks": sum(summary.failed for summary in summaries),
            "retry_waiting_tasks": sum(summary.retry_waiting for summary in summaries),
            "blocked_tasks": sum(summary.blocked for summary in summaries),
        }

    def _coerce_stage(self, stage: IntelRunStage | int) -> IntelRunStage | None:
        if isinstance(stage, IntelRunStage):
            return stage
        try:
            return self.session.get(IntelRunStage, int(stage))
        except (TypeError, ValueError):
            return None

    def _stage_id(self, stage: IntelRunStage | int) -> int | None:
        row = self._coerce_stage(stage)
        return row.id if row is not None else None

    def _current_attempt(self, task: IntelRunStageTask) -> IntelRunStageAttempt | None:
        if task.last_attempt_id:
            attempt = self.session.get(IntelRunStageAttempt, task.last_attempt_id)
            if attempt is not None:
                return attempt
        return self.session.scalar(
            select(IntelRunStageAttempt)
            .where(IntelRunStageAttempt.task_id == task.id)
            .order_by(IntelRunStageAttempt.attempt_no.desc())
        )

    def _finish_attempt_row(self, attempt: IntelRunStageAttempt, *, status: str, finished_at: datetime, **kwargs: Any) -> None:
        attempt.status = status
        attempt.finished_at = finished_at
        if "retryable" in kwargs and kwargs["retryable"] is not None:
            attempt.retryable = bool(kwargs["retryable"])
        if "next_retry_at" in kwargs:
            attempt.next_retry_at = kwargs["next_retry_at"]
        for name in ("error_category", "error_code", "error_message"):
            if name in kwargs and kwargs[name] is not None:
                setattr(attempt, name, _text(kwargs[name]))
        if kwargs.get("result_ref") is not None:
            attempt.result_ref_json = _dump_json(_structured_json(kwargs["result_ref"]))
        metadata = kwargs.get("metadata")
        if metadata is not None:
            attempt.metadata_json = _dump_json(dict(metadata) if isinstance(metadata, Mapping) else _structured_json(metadata))
        raw_response = kwargs.get("raw_response")
        # Immutable raw audit: the first completed payload wins and can never
        # be overwritten by a later retry or cleanup call.
        if raw_response is not None and not attempt.raw_response_json:
            payload = _structured_json(raw_response)
            attempt.raw_response_json = _dump_json(payload)
            attempt.raw_response_hash = hashlib.sha256(attempt.raw_response_json.encode("utf-8")).hexdigest()
        attempt.lease_owner = None
        attempt.lease_expires_at = None
        attempt.heartbeat_at = None

    def _reset_task(self, task: IntelRunStageTask, *, include_succeeded: bool = False, reset_attempt_count: bool = False) -> None:
        if task.status == "succeeded" and not include_succeeded:
            return
        task.status = "pending"
        task.next_retry_at = None
        task.lease_owner = None
        task.lease_expires_at = None
        task.heartbeat_at = None
        task.result_ref_json = "{}"
        task.result_json = "{}"
        task.error_category = None
        task.error_code = None
        task.error_message = None
        if reset_attempt_count:
            task.attempt_count = 0
        task.updated_at = utcnow()

    def _reset_stage_tasks(self, stage_id: int, *, include_succeeded: bool = False, reset_attempt_count: bool = False) -> None:
        rows = list(self.session.scalars(select(IntelRunStageTask).where(IntelRunStageTask.stage_id == int(stage_id))).all())
        for task in rows:
            self._reset_task(task, include_succeeded=include_succeeded, reset_attempt_count=reset_attempt_count)

    def count_by_status(self) -> dict[str, int]:
        rows = self.session.execute(
            select(IntelItem.status).order_by(IntelItem.status)
        ).all()
        counts: dict[str, int] = {}
        for (status,) in rows:
            counts[status] = counts.get(status, 0) + 1
        return counts


def _run_item_roles(value: str | Iterable[str]) -> list[str]:
    values = [value] if isinstance(value, str) else value
    result: list[str] = []
    for item in values:
        role = _text(item)
        if role and role not in result:
            result.append(role)
    return result or [RUN_ITEM_ROLE_FETCHED]


def _item_has_material_change(
    existing: IntelItem,
    fields: Mapping[str, Any],
) -> bool:
    """Whether the inbound record contains a new editorial signal.

    Fetch payloads often update mutable counters, crawl metadata and raw JSON
    on every invocation. Those changes remain persisted for audit but do not
    qualify an old story for another daily edition. GitHub repository hashes
    are deliberately identity-stable, so their metric-only refreshes are also
    intentionally excluded here.
    """

    is_github_repository = _is_github_repository_fields(fields)
    next_title = fields["title"] or existing.title
    next_summary = fields["summary"] or existing.summary
    next_url = fields["canonical_url"] or existing.canonical_url
    next_published_at = fields["published_at"] or existing.published_at

    if existing.content_class != fields["content_class"]:
        return True
    if existing.title != next_title or existing.summary != next_summary or existing.canonical_url != next_url:
        return True
    if _as_utc(existing.published_at) != _as_utc(next_published_at):
        return True
    if not is_github_repository and fields["content_hash"] and existing.content_hash != fields["content_hash"]:
        return True
    return False


def _item_fields(item: Any) -> dict[str, Any]:
    values = _object_mapping(item)
    source_id = _text(values.get("source_id")) or "unknown"
    external_id = _text(values.get("external_id"))
    canonical_url = _canonical_url(values.get("canonical_url") or values.get("link") or values.get("url"))
    title = _text(values.get("title")) or "(untitled)"
    summary = _text(values.get("summary") or values.get("raw_summary"))
    content_text = _text(values.get("content_text") or values.get("content") or values.get("raw_content") or summary)
    content_class = _text(values.get("content_class"))
    if content_class == "project_tool":
        github_url = _canonical_github_url(canonical_url)
        if github_url and (
            (external_id or "").casefold().startswith("github_repo:")
            or github_url.casefold().startswith("https://github.com/")
        ):
            canonical_url = github_url
    metrics = values.get("metrics") or {}
    raw_payload = values.get("raw_payload") or values.get("raw_payload_json") or {}
    if isinstance(metrics, str):
        metrics = _load_json(metrics, {})
    if isinstance(raw_payload, str):
        raw_payload = _load_json(raw_payload, {})
    if not isinstance(metrics, dict):
        metrics = {}
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    published_at = _as_utc(values.get("published_at"))
    captured_at = _as_utc(values.get("captured_at")) or utcnow()
    discovered_at = _as_utc(values.get("discovered_at")) or captured_at
    original_title = _text(values.get("original_title") or values.get("title"))
    source_url = _canonical_url(values.get("source_url") or values.get("url") or values.get("link"))
    content_depth = _text(values.get("content_depth")) or ("full" if values.get("content_text") or values.get("content") else "summary")
    content_hash = _text(values.get("content_hash")) or _identity_hash(external_id, canonical_url, title, content_text)
    # Keep GitHub repository identity stable as star/description metrics change.
    if external_id and external_id.startswith("github_repo:"):
        content_hash = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    return {
        "source_id": source_id,
        "external_id": external_id,
        "canonical_url": canonical_url,
        "title": title,
        "summary": summary,
        "content_text": content_text,
        "published_at": published_at,
        "captured_at": captured_at,
        "discovered_at": discovered_at,
        "original_title": original_title,
        "source_url": source_url,
        "content_depth": content_depth,
        "content_class": content_class,
        "metrics": metrics,
        "raw_payload": raw_payload,
        "content_hash": content_hash,
    }


def _object_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _policy_dict(policy: Any, kind: str) -> dict[str, Any]:
    if isinstance(policy, Mapping):
        value = policy.get(f"{kind}_policy", {})
        return dict(value) if isinstance(value, Mapping) else {}
    value = getattr(policy, f"{kind}_policy", None)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(exclude_none=True)
        if isinstance(dumped, Mapping):
            return dict(dumped)
    if hasattr(policy, "to_dict"):
        data = policy.to_dict()
        value = data.get(f"{kind}_policy", {})
        return dict(value) if isinstance(value, Mapping) else {}
    return {}


def _counts_dict(counts: IntelCounts | Mapping[str, Any] | None) -> dict[str, Any]:
    if counts is None:
        return {}
    if isinstance(counts, Mapping):
        result: dict[str, Any] = {}
        for key, value in counts.items():
            name = str(key)
            if name == "partial_reason":
                result[name] = value
                continue
            try:
                result[name] = int(value)
            except (TypeError, ValueError, OverflowError):
                result[name] = 0
        return result
    return {key: int(value) for key, value in asdict(counts).items()}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _normalize_subject_type(value: Any) -> str:
    text = (_text(value) or "item").casefold().replace("-", "_")
    aliases = {
        "items": "item",
        "event_item": "event",
        "events": "event",
        "runs": "run",
        "run_stage": "run",
    }
    return aliases.get(text, text)


def _retryable_stage_error(category: str | None, code: str | None, message: str | None) -> bool:
    """Apply the frozen provider retry policy conservatively."""

    values = " ".join(value.casefold() for value in (category, code, message) if value)
    if any(token in values for token in ("auth", "unauthorized", "forbidden", "schema", "validation", "4xx", "400", "401", "403", "404")):
        return False
    return any(token in values for token in ("429", "rate_limit", "ratelimit", "timeout", "timed out", "5xx", "500", "502", "503", "504", "temporarily", "unavailable", "retry"))


def _projection_fingerprint(projection: Any) -> str:
    model = getattr(projection, "model", None)
    prompt_version = getattr(projection, "prompt_version", None)
    status = getattr(projection, "status", None)
    return hashlib.sha256(
        _dump_json({"model": model, "prompt_version": prompt_version, "status": status}).encode("utf-8")
    ).hexdigest()


def _response_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _structured_json(value: Any) -> Any:
    """Convert Pydantic/dataclass values before JSON text persistence."""

    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if isinstance(value, Mapping):
        return {str(key): _structured_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_structured_json(item) for item in value]
    return value


def _bounded_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_screen_decision(value: Any) -> str:
    text = (_text(value) or "").casefold()
    return text if text in {"pass", "reject", "uncertain"} else "uncertain"


def _unique_ints(values: Iterable[Any]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if number in seen:
            continue
        seen.add(number)
        result.append(number)
    return result


def _unique_json_objects(values: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            continue
        normalized = dict(_structured_json(value))
        marker = _dump_json(normalized)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(normalized)
    return result


def _normalize_event_title(value: Any) -> str:
    """Normalize title identity without changing the display title."""

    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value).strip()).casefold()
    # Keep Unicode letters/numbers (including Chinese) while dropping
    # punctuation and feed-specific separators.  This makes ``Foo – v1`` and
    # ``foo v1`` share an exact title identity without fuzzy matching.
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_event_external_id(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return re.sub(r"\s+", "", text).casefold()


def _identity_alias_url(value: str | None) -> str | None:
    return f"url:{value}" if value else None


def _identity_alias_external(value: str | None) -> str | None:
    return f"external:{value}" if value else None


def _normalize_novelty_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold().replace("-", "_")
    aliases = {
        "new_item": "new",
        "novel": "new",
        "fresh": "new",
        "update": "updated",
        "updated": "updated",
        "version_update": "updated",
        "duplicate": "repeat",
        "old": "repeat",
        "undetermined": "unknown",
        "": "unknown",
    }
    text = aliases.get(text, text)
    return text if text in {"new", "updated", "repeat", "unknown"} else "unknown"


def _load_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _is_github_repository_fields(fields: Mapping[str, Any]) -> bool:
    external_id = _text(fields.get("external_id")) or ""
    return external_id.casefold().startswith("github_repo:") and fields.get("content_class") == "project_tool"


def _merge_github_project_metrics(
    previous: Any,
    current: Any,
    *,
    source_id: str,
) -> dict[str, Any]:
    """Merge current GitHub signals without losing other source periods."""

    merged = dict(previous) if isinstance(previous, Mapping) else {}
    values = dict(current) if isinstance(current, Mapping) else {}
    period = _text(values.get("trending_period"))
    if period:
        trending = dict(merged.get("trending")) if isinstance(merged.get("trending"), Mapping) else {}
        previous_period = trending.get(period) if isinstance(trending.get(period), Mapping) else {}
        period_stars = values.get("stars")
        period_forks = values.get("forks")
        if _github_metric_number(previous_period.get("stars")) is not None and (
            _github_metric_number(period_stars) or 0.0
        ) < (_github_metric_number(previous_period.get("stars")) or 0.0):
            period_stars = previous_period.get("stars")
        if _github_metric_number(previous_period.get("forks")) is not None and (
            _github_metric_number(period_forks) or 0.0
        ) < (_github_metric_number(previous_period.get("forks")) or 0.0):
            period_forks = previous_period.get("forks")
        trending[period] = {
            "rank": values.get("trending_rank") if values.get("trending_rank") is not None else previous_period.get("rank"),
            "stars_since": values.get("stars_since") if values.get("stars_since") is not None else previous_period.get("stars_since"),
            "stars": period_stars if period_stars is not None else previous_period.get("stars"),
            "forks": period_forks if period_forks is not None else previous_period.get("forks"),
        }
        merged["trending"] = trending
        # Keep the strongest period signal at the canonical top level too. The
        # selection policy reads this fast path, while ``trending`` preserves
        # daily/weekly history within the current record. A later daily refresh
        # therefore cannot erase a stronger weekly signal.
        best_period = max(
            trending.items(),
            key=lambda entry: (_github_metric_number(entry[1].get("stars_since")) or 0.0, entry[0]),
        )[0]
        merged["trending_period"] = best_period
        merged["trending_rank"] = trending[best_period].get("rank")
        merged["stars_since"] = trending[best_period].get("stars_since")
        merged["trending_signal"] = "stars_since"

    for key, value in values.items():
        if key in {"trending_period", "trending_rank", "stars_since", "trending_signal"}:
            continue
        if key in {"stars", "forks", "watchers", "open_issues"}:
            previous_number = _github_metric_number(merged.get(key))
            current_number = _github_metric_number(value)
            if current_number is not None and (previous_number is None or current_number >= previous_number):
                merged[key] = value
            elif previous_number is None and value is not None:
                merged[key] = value
            continue
        if key == "topics":
            merged[key] = _unique_strings([*_string_values(merged.get(key)), *_string_values(value)])
            continue
        if value is not None:
            merged[key] = value

    merged["discovery_sources"] = _unique_strings(
        [
            *_string_values(merged.get("discovery_sources")),
            *_string_values(values.get("discovery_sources")),
            source_id,
        ]
    )
    query_values = [
        merged.get("search_query"),
        merged.get("query"),
        values.get("search_query"),
        values.get("query"),
    ]
    queries = _unique_strings(query_values)
    if queries:
        merged["search_queries"] = queries
    if source_id.startswith("github_search_topic_"):
        topic = source_id.removeprefix("github_search_topic_")
        merged["search_topics"] = _unique_strings([*_string_values(merged.get("search_topics")), topic])
    query_topics: list[str] = []
    for query in queries:
        query_topics.extend(re.findall(r"(?:^|\s)topic:([^\s]+)", query, flags=re.IGNORECASE))
    if query_topics:
        merged["search_topics"] = _unique_strings([*_string_values(merged.get("search_topics")), *query_topics])
    return merged


def _github_metadata_metrics(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Select bounded, displayable repository fields from GitHub metadata."""

    license_value = metadata.get("license")
    if isinstance(license_value, Mapping):
        license_value = license_value.get("spdx_id") or license_value.get("name")
    topics = metadata.get("topics")
    return {
        key: value
        for key, value in {
            "stars": metadata.get("stargazers_count"),
            "forks": metadata.get("forks_count"),
            "watchers": metadata.get("watchers_count"),
            "open_issues": metadata.get("open_issues_count"),
            "language": metadata.get("language"),
            "topics": list(topics)[:100] if isinstance(topics, list) else None,
            "description": (_text(metadata.get("description")) or "")[:4_000] or None,
            "full_name": _text(metadata.get("full_name")),
            "canonical_project_key": _text(metadata.get("full_name")),
            "pushed_at": metadata.get("pushed_at"),
            "updated_at": metadata.get("updated_at"),
            "created_at": metadata.get("created_at"),
            "archived": metadata.get("archived"),
            "fork": metadata.get("fork"),
            "license": license_value,
            "default_branch": metadata.get("default_branch"),
        }.items()
        if value is not None
    }


def _bounded_github_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only display/enrichment fields and cap free-form description text."""

    allowed = {
        "full_name", "html_url", "description", "topics", "stargazers_count",
        "forks_count", "watchers_count", "open_issues_count", "language",
        "pushed_at", "updated_at", "created_at", "archived", "fork", "license",
        "default_branch", "readme_url",
    }
    bounded = {key: metadata[key] for key in allowed if key in metadata}
    if "description" in bounded:
        bounded["description"] = (_text(bounded.get("description")) or "")[:4_000] or None
    if isinstance(bounded.get("topics"), list):
        bounded["topics"] = [str(topic)[:128] for topic in bounded["topics"][:100] if str(topic).strip()]
    return bounded


def _merge_github_raw_payload(previous: Any, current: Any) -> dict[str, Any]:
    merged = dict(previous) if isinstance(previous, Mapping) else {}
    if isinstance(current, Mapping):
        merged.update(dict(current))
    return merged


def _canonical_github_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (parsed.hostname or "").casefold() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not owner or not repo:
        return None
    return f"https://github.com/{owner.casefold()}/{repo.casefold()}"


def _github_metric_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return result


def _string_values(value: Any) -> list[Any]:
    """Normalize scalar/list metadata before merging it."""

    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _as_edition_date(value: Any) -> date | None:
    """Parse a public daily identifier without coupling it to run IDs."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _build_scope(column, build_id: int):
    """Return the SQL predicate for one private build namespace."""

    return column == int(build_id)


def _identity_hash(external_id: str | None, url: str | None, title: str, content: str | None) -> str:
    identity = external_id or url or f"{title.lower()}\n{content or ''}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _canonical_url(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return text
    host = parsed.hostname.lower() if parsed.hostname else parsed.netloc.lower()
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        return text
    if port is not None and not (
        (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", parsed.query))
