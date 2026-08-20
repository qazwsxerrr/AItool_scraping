"""SQLAlchemy models for the date-addressed intelligence pipeline.

Transient build data belongs to one hidden build, while published reports are
stored only as date-addressed daily editions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


DAILY_EDITION_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _dump_json(value: object) -> str:
    """Serialize state metadata consistently with the existing projections."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _decode_json(value: str | None, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if parsed is not None else default


def _decode_list(value: str | None) -> list[str]:
    parsed = _decode_json(value, [])
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``transport`` is the only persisted routing discriminator. Feed and
    # GitHub options are flattened into nullable columns so the local SQLite
    # schema remains inspectable without a migration layer.
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    fetch_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    default_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    feed_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    feed_adapter: Mapped[str | None] = mapped_column(String(32), nullable=True)
    github_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    github_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_sort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    github_order: Mapped[str | None] = mapped_column(String(8), nullable=True)
    github_pushed_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_group: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    source_subtype: Mapped[str] = mapped_column(String(64), nullable=False, default="fixed")
    account_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Governance metadata has safe defaults for incomplete source specs.
    tier: Mapped[str] = mapped_column(String(8), nullable=False, default="p4")
    topic_scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    primary_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quality_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spam_risk: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_class: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Conditional request and source-health state consumed by fetch jobs.
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    @property
    def topic_scopes(self) -> list[str]:
        """Decoded governance scopes for callers that prefer a Python list."""

        try:
            import json

            value = json.loads(self.topic_scopes_json or "[]")
            return [str(item) for item in value] if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []

    intel_items: Mapped[list["IntelItem"]] = relationship(back_populates="source")
    fetch_attempts: Mapped[list["FetchAttempt"]] = relationship(back_populates="source")


class FetchAttempt(Base):
    """Durable telemetry for one source request."""

    __tablename__ = "fetch_attempts"
    __table_args__ = (
        Index("ix_fetch_attempts_source_started", "source_id", "started_at"),
        Index("ix_fetch_attempts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("intel_runs.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    manual_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transport: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[Source] = relationship(back_populates="fetch_attempts")


class DailyEdition(Base):
    """The single public daily workspace addressed by ``edition_date``.

    An edition is the only business-facing identity for a daily report.  The
    Builds are kept in a separate draft database, so this published model has
    no pointer to a mutable run. All user-facing readers use the date and the
    persisted report entries below instead.
    """

    __tablename__ = "daily_editions"
    __table_args__ = (
        UniqueConstraint("edition_date", name="uq_daily_editions_edition_date"),
        Index("ix_daily_editions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="empty")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    report_entries: Mapped[list["DailyEditionReportEntry"]] = relationship(
        back_populates="edition", cascade="all, delete-orphan", order_by="DailyEditionReportEntry.display_order"
    )


class DailyEditionReportEntry(Base):
    """Published event payload retained after a build's working data is removed."""

    __tablename__ = "daily_edition_report_entries"
    __table_args__ = (
        UniqueConstraint("edition_id", "display_order", name="uq_daily_edition_report_order"),
        UniqueConstraint("edition_id", "event_key", name="uq_daily_edition_report_event"),
        Index("ix_daily_edition_report_entries_edition", "edition_id"),
        Index("ix_daily_edition_report_entries_event_key", "event_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("daily_editions.id"), nullable=False)
    event_key: Mapped[str] = mapped_column(String(512), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="(untitled)")
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    topic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    entities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    edition: Mapped[DailyEdition] = relationship(back_populates="report_entries")

    @property
    def source_ids(self) -> list[str]:
        return _decode_list(self.source_ids_json)

    @property
    def source_refs(self) -> list[dict[str, object]]:
        value = _decode_json(self.source_refs_json, [])
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @property
    def risk_flags(self) -> list[str]:
        return _decode_list(self.risk_flags_json)

    @property
    def keywords(self) -> list[str]:
        return _decode_list(self.keywords_json)

    @property
    def entities(self) -> list[dict[str, object]]:
        value = _decode_json(self.entities_json, [])
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @property
    def metadata_dict(self) -> dict[str, object]:
        value = _decode_json(self.metadata_json, {})
        return dict(value) if isinstance(value, dict) else {}


class IntelRun(Base):
    """Summary and item scope for one intelligence run.

    A run is intentionally a durable processing boundary.  Stage jobs record
    the IDs fetched by that run through :class:`IntelRunItem`; downstream
    stages can therefore query only the current scope instead of scanning all
    historical items.  The explicit counters make partial/capped runs
    auditable without deriving state from provider payloads.
    """

    __tablename__ = "intel_runs"
    __table_args__ = (
        Index("ix_intel_runs_status", "status"),
        Index("ix_intel_runs_started_at", "started_at"),
        Index("ix_intel_runs_edition_id", "edition_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ``edition_id`` is an opaque build-owner pointer.  The public daily key
    # lives in ``daily_editions.edition_date`` and is intentionally unique.
    edition_id: Mapped[int] = mapped_column(ForeignKey("daily_editions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    item_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    screened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    screened_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    screen_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analyzed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analysis_filtered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analysis_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    partial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ``selected`` is a build-local aggregate maintained for stage summaries.
    selected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    run_items: Mapped[list["IntelRunItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    run_stages: Mapped[list["IntelRunStage"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="IntelRunStage.id"
    )
    edition: Mapped[DailyEdition] = relationship(foreign_keys=[edition_id], lazy="joined")

    @property
    def scope(self) -> dict[str, object]:
        value = _decode_json(self.scope_json, {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def scope_frozen(self) -> bool:
        """Whether fetch membership has been frozen for downstream stages."""

        return bool(self.scope.get("_frozen"))

    @property
    def scope_frozen_at(self) -> datetime | None:
        value = self.scope.get("_frozen_at")
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @property
    def source_ids(self) -> list[str]:
        return _decode_list(self.source_ids_json)

    @property
    def item_ids(self) -> list[int]:
        value = _decode_json(self.item_ids_json, [])
        result: list[int] = []
        if isinstance(value, list):
            for item in value:
                try:
                    result.append(int(item))
                except (TypeError, ValueError):
                    continue
        return result

    @property
    def reference_time(self) -> datetime | None:
        """Fixed run reference time used by resumable downstream stages.

        Persisting the value in ``scope_json`` keeps build retries stable
        without exposing the hidden build ID to daily callers.
        """

        value = self.scope.get("reference_time")
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @reference_time.setter
    def reference_time(self, value: datetime | None) -> None:
        scope = self.scope
        if value is None:
            scope.pop("reference_time", None)
        else:
            current = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            scope["reference_time"] = current.isoformat()
        self.scope_json = _dump_json(scope)

    @property
    def edition_date(self) -> str | None:
        """Date of the build's owning daily edition."""

        return self.edition.edition_date.isoformat() if self.edition is not None else None

class IntelRunStage(Base):
    """Durable state for one named stage within an :class:`IntelRun`.

    Stage rows are the coordinator's source of truth for one hidden build.
    """

    __tablename__ = "intel_run_stages"
    __table_args__ = (
        UniqueConstraint("run_id", "stage_name", name="uq_intel_run_stages_run_stage"),
        Index("ix_intel_run_stages_run_status", "run_id", "status"),
        Index("ix_intel_run_stages_status", "status"),
        Index("ix_intel_run_stages_lease", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("intel_runs.id"), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    input_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reference_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_ref_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    run: Mapped[IntelRun] = relationship(back_populates="run_stages")
    tasks: Mapped[list["IntelRunStageTask"]] = relationship(
        back_populates="stage", cascade="all, delete-orphan", order_by="IntelRunStageTask.id"
    )

    @property
    def result_ref(self) -> object:
        return _decode_json(self.result_ref_json, {})

    @result_ref.setter
    def result_ref(self, value: object) -> None:
        self.result_ref_json = _dump_json(value if value is not None else {})

    @property
    def metadata_dict(self) -> dict[str, object]:
        value = _decode_json(self.metadata_json, {})
        return dict(value) if isinstance(value, dict) else {}

    @metadata_dict.setter
    def metadata_dict(self, value: object) -> None:
        self.metadata_json = _dump_json(value if value is not None else {})

    @property
    def lease_active(self) -> bool:
        current = utcnow()
        return bool(self.lease_owner and self.lease_expires_at and self.lease_expires_at > current)

class IntelRunStageTask(Base):
    """One independently resumable stage unit.

    ``subject_type``/``subject_id`` support item, event, and run subjects.
    Optional typed foreign keys make common queries explicit without forcing
    callers to encode IDs in JSON.
    """

    __tablename__ = "intel_run_stage_tasks"
    __table_args__ = (
        UniqueConstraint(
            "stage_id", "subject_type", "subject_id", name="uq_intel_run_stage_tasks_subject"
        ),
        Index("ix_intel_run_stage_tasks_stage_status", "stage_id", "status"),
        Index("ix_intel_run_stage_tasks_retry", "stage_id", "status", "next_retry_at"),
        Index("ix_intel_run_stage_tasks_lease", "lease_expires_at"),
        Index("ix_intel_run_stage_tasks_subject", "subject_type", "subject_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("intel_run_stages.id"), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False, default="item")
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False)
    # Explicit references are optional and are kept in sync by the repository
    # when the subject is an item/event/run.  They are useful for joins and
    # leave generic subjects available for future stages.
    item_id: Mapped[int | None] = mapped_column(ForeignKey("intel_items.id"), nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("intel_events.id"), nullable=True)
    target_run_id: Mapped[int | None] = mapped_column(ForeignKey("intel_runs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    input_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("intel_run_stage_attempts.id"), nullable=True)
    result_ref_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    stage: Mapped[IntelRunStage] = relationship(back_populates="tasks", foreign_keys=[stage_id])
    attempts: Mapped[list["IntelRunStageAttempt"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="IntelRunStageAttempt.attempt_no",
        foreign_keys="IntelRunStageAttempt.task_id",
    )

    @property
    def result_ref(self) -> object:
        return _decode_json(self.result_ref_json, {})

    @result_ref.setter
    def result_ref(self, value: object) -> None:
        self.result_ref_json = _dump_json(value if value is not None else {})

    @property
    def result(self) -> object:
        return _decode_json(self.result_json, {})

    @result.setter
    def result(self, value: object) -> None:
        self.result_json = _dump_json(value if value is not None else {})

    @property
    def lease_active(self) -> bool:
        current = utcnow()
        return bool(self.lease_owner and self.lease_expires_at and self.lease_expires_at > current)

    @property
    def reusable(self) -> bool:
        return self.status == "succeeded"

class IntelRunStageAttempt(Base):
    """Immutable-attempt audit row for one provider/local execution."""

    __tablename__ = "intel_run_stage_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no", name="uq_intel_run_stage_attempts_task_no"),
        Index("ix_intel_run_stage_attempts_task", "task_id", "attempt_no"),
        Index("ix_intel_run_stage_attempts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("intel_run_stage_tasks.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_ref_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # This payload is append-only audit data.  Repository finish methods never
    # replace a non-empty value, preserving the first provider response.
    raw_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    task: Mapped[IntelRunStageTask] = relationship(
        back_populates="attempts", foreign_keys=[task_id]
    )

    @property
    def result_ref(self) -> object:
        return _decode_json(self.result_ref_json, {})

    @result_ref.setter
    def result_ref(self, value: object) -> None:
        self.result_ref_json = _dump_json(value if value is not None else {})

    @property
    def raw_response(self) -> object:
        return _decode_json(self.raw_response_json, {})

    @raw_response.setter
    def raw_response(self, value: object) -> None:
        payload = _dump_json(value if value is not None else {})
        self.raw_response_json = payload
        self.raw_response_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def metadata_dict(self) -> dict[str, object]:
        value = _decode_json(self.metadata_json, {})
        return dict(value) if isinstance(value, dict) else {}


class IntelRunItem(Base):
    """Run-local scope relation retaining every fetched item lineage."""

    __tablename__ = "intel_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="uq_intel_run_items_run_item"),
        Index("ix_intel_run_items_run", "run_id"),
        Index("ix_intel_run_items_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("intel_runs.id"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="fetched")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="fetched")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    run: Mapped[IntelRun] = relationship(back_populates="run_items")
    item: Mapped["IntelItem"] = relationship(back_populates="run_items")
    source: Mapped[Source | None] = relationship()


class IntelItem(Base):
    """Unified normalized item produced by a collector."""

    __tablename__ = "intel_items"
    __table_args__ = (
        # Daily builds are fully isolated workspaces.  The same source item
        # may legitimately be fetched and re-evaluated on two edition dates,
        # so identities are unique only inside one hidden build.
        UniqueConstraint("build_id", "source_id", "external_id", name="uq_intel_items_build_source_external_id"),
        UniqueConstraint("build_id", "source_id", "content_hash", name="uq_intel_items_build_source_content_hash"),
        Index("ix_intel_items_status", "status"),
        Index("ix_intel_items_build", "build_id"),
        Index("ix_intel_items_content_class", "content_class"),
        Index("ix_intel_items_published_at", "published_at"),
        Index("ix_intel_items_selection_score", "selection_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Every persisted raw item belongs to one hidden daily build.
    build_id: Mapped[int] = mapped_column(ForeignKey("intel_runs.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_depth: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_class: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    discovered_links_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    source: Mapped[Source] = relationship(back_populates="intel_items")
    run_items: Mapped[list[IntelRunItem]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    ai_screen: Mapped["AIItemScreen | None"] = relationship(back_populates="item", uselist=False)
    ai_review: Mapped["AIItemReview | None"] = relationship(back_populates="item", uselist=False)
    event_items: Mapped[list["IntelEventItem"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
    )


class AIItemScreen(Base):
    """Durable Stage A screen result and raw provider audit payload."""

    __tablename__ = "ai_item_screens"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_ai_item_screens_item_id"),
        Index("ix_ai_item_screens_status", "status"),
        Index("ix_ai_item_screens_decision", "decision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="intel_screen_v1")
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="uncertain")
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    item: Mapped[IntelItem] = relationship(back_populates="ai_screen")

    @property
    def risk_flags(self) -> list[str]:
        return _decode_list(self.risk_flags_json)

    @property
    def raw_response(self) -> dict[str, object]:
        value = _decode_json(self.raw_response_json, {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def raw_payload(self) -> dict[str, object]:
        return self.raw_response


class AIItemReview(Base):
    """At most one structured Stage B analysis projection for each item."""

    __tablename__ = "ai_item_reviews"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_ai_item_reviews_item_id"),
        Index("ix_ai_item_reviews_status", "status"),
        Index("ix_ai_item_reviews_topic", "topic"),
        Index("ix_ai_item_reviews_score", "selection_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="intel_analysis_v1")
    content_class: Mapped[str] = mapped_column(String(64), nullable=False)
    topic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topics_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    entities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    selection_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_components_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    item: Mapped[IntelItem] = relationship(back_populates="ai_review")

    @staticmethod
    def _decode_json(value: str, default):
        try:
            parsed = json.loads(value or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return parsed if parsed is not None else default

    @property
    def topics(self) -> list[str]:
        value = self._decode_json(self.topics_json, [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def keywords(self) -> list[str]:
        value = self._decode_json(self.keywords_json, [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def scores(self) -> dict[str, object]:
        value = self._decode_json(self.score_components_json, {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def score_components(self) -> dict[str, object]:
        return self.scores

    @property
    def entities(self) -> list[dict[str, object]]:
        value = self._decode_json(self.entities_json, [])
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @property
    def raw_response(self) -> dict[str, object]:
        value = self._decode_json(self.raw_response_json, {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def raw_payload(self) -> dict[str, object]:
        return self.raw_response


class IntelEvent(Base):
    """Canonical event assembled from one or more normalized items.

    Event identity is deliberately separate from item selection.  The event
    row stores the strongest exact identity seen for the group and a JSON list
    of all identity aliases, so a later URL/external-id discovery can update an
    existing event without creating a duplicate on rerun.
    """

    __tablename__ = "intel_events"
    __table_args__ = (
        # Event rows are draft-private for the same reason as IntelItem rows:
        # cross-edition repeat handling reads the published report table,
        # never a retained historical event row.
        UniqueConstraint("build_id", "event_key", name="uq_intel_events_build_event_key"),
        Index("ix_intel_events_build", "build_id"),
        Index("ix_intel_events_state", "state"),
        Index("ix_intel_events_topic", "topic"),
        Index("ix_intel_events_novelty_status", "novelty_status"),
        Index("ix_intel_events_display_score", "display_score"),
        Index("ix_intel_events_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(ForeignKey("intel_runs.id"), nullable=False)
    event_key: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_title: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="(untitled)")
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    topics_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    entities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_groups_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    identity_keys_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    display_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    novelty_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    resolution_method: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    resolution_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    resolution_raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    primary_item_id: Mapped[int | None] = mapped_column(ForeignKey("intel_items.id"), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    event_items: Mapped[list["IntelEventItem"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="IntelEventItem.id",
    )
    stage_d_snapshots: Mapped[list["IntelEventStageDSnapshot"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="IntelEventStageDSnapshot.display_order",
    )

    @property
    def novelty(self) -> str:
        return self.novelty_status

    @property
    def keywords(self) -> list[str]:
        return _decode_list(self.keywords_json)

    @property
    def entities(self) -> list[dict[str, object]]:
        value = _decode_json(self.entities_json, [])
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class IntelEventItem(Base):
    """Lineage relation between an event and every contributing item."""

    __tablename__ = "intel_event_items"
    __table_args__ = (
        UniqueConstraint("event_id", "item_id", name="uq_intel_event_items_event_item"),
        UniqueConstraint("item_id", name="uq_intel_event_items_item_id"),
        Index("ix_intel_event_items_event", "event_id"),
        Index("ix_intel_event_items_source", "source_id"),
        Index("ix_intel_event_items_match_type", "match_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("intel_events.id"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    source_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    match_type: Mapped[str] = mapped_column(String(32), nullable=False, default="deterministic")
    match_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lineage_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    event: Mapped[IntelEvent] = relationship(back_populates="event_items")
    item: Mapped[IntelItem] = relationship(back_populates="event_items")
    source: Mapped[Source] = relationship()


class IntelEventStageDSnapshot(Base):
    """Private Stage-D output for one hidden daily build."""

    __tablename__ = "intel_event_stage_d_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "event_id", name="uq_intel_event_stage_d_snapshot_run_event"),
        Index("ix_intel_event_stage_d_snapshot_run_order", "run_id", "display_order"),
        Index("ix_intel_event_stage_d_snapshot_run_selected", "run_id", "selected"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("intel_events.id"), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("intel_runs.id"), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    display_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    topic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    event: Mapped[IntelEvent] = relationship(back_populates="stage_d_snapshots")
