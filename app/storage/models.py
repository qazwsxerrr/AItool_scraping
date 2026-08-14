"""SQLAlchemy models for the compact AI-only intelligence pipeline.

The data layer contains sources, fetch telemetry, runs, normalized items,
structured AI reviews, and the Wave 2 event/member/ranking snapshot tables.
Historical stage tables are not migrated; the local SQLite database may be
recreated from this metadata.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    # V3 governance metadata.  These columns deliberately have safe defaults
    # so existing callers that construct ``Source`` rows directly remain
    # compatible with a freshly-created database.
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
    run_id: Mapped[int | None] = mapped_column(ForeignKey("intel_runs.id"), nullable=True)
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


class IntelRun(Base):
    """Summary of one v2 run or one individually invoked stage."""

    __tablename__ = "intel_runs"
    __table_args__ = (
        Index("ix_intel_runs_status", "status"),
        Index("ix_intel_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analyzed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class IntelItem(Base):
    """Unified normalized item produced by a collector."""

    __tablename__ = "intel_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_intel_items_source_external_id"),
        UniqueConstraint("content_hash", name="uq_intel_items_content_hash"),
        Index("ix_intel_items_status", "status"),
        Index("ix_intel_items_content_class", "content_class"),
        Index("ix_intel_items_published_at", "published_at"),
        Index("ix_intel_items_selection_score", "selection_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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
    ai_review: Mapped["AIItemReview | None"] = relationship(back_populates="item", uselist=False)
    event_items: Mapped[list["IntelEventItem"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
    )


class AIItemReview(Base):
    """At most one structured model result for each intelligence item.

    The Wave 1 triage contract is stored explicitly instead of requiring event
    clustering to reverse-engineer fields from ``raw_response_json``.  JSON is
    kept as text to preserve the repository's SQLite-first schema and to avoid
    a dialect-specific migration; event jobs decode these fields through the
    repository boundary.
    """

    __tablename__ = "ai_item_reviews"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_ai_item_reviews_item_id"),
        Index("ix_ai_item_reviews_status", "status"),
        Index("ix_ai_item_reviews_keep", "keep"),
        Index("ix_ai_item_reviews_topic", "topic"),
        Index("ix_ai_item_reviews_novelty", "novelty"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="item_analysis_v1")
    keep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_class: Mapped[str] = mapped_column(String(64), nullable=False)
    # Explicit Wave 1 Intel Triage fields.  Nullable values preserve
    # compatibility with historical ItemAnalysis rows that predate triage.
    topic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topics_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    keywords_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    selection_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scores_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    novelty: Mapped[str | None] = mapped_column(String(16), nullable=True)
    novelty_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paper_support_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
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
        value = self._decode_json(self.scores_json, {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def paper_support(self) -> dict[str, object]:
        value = self._decode_json(self.paper_support_json, {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def risk_flags(self) -> list[str]:
        value = self._decode_json(self.risk_flags_json, [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def raw_response(self) -> dict[str, object]:
        value = self._decode_json(self.raw_response_json, {})
        return dict(value) if isinstance(value, dict) else {}


class IntelEvent(Base):
    """Canonical event assembled from one or more normalized items.

    Event identity is deliberately separate from item selection.  The event
    row stores the strongest exact identity seen for the group and a JSON list
    of all identity aliases, so a later URL/external-id discovery can update an
    existing event without creating a duplicate on rerun.
    """

    __tablename__ = "intel_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_intel_events_event_key"),
        Index("ix_intel_events_state", "state"),
        Index("ix_intel_events_topic", "topic"),
        Index("ix_intel_events_novelty_status", "novelty_status"),
        Index("ix_intel_events_display_score", "display_score"),
        Index("ix_intel_events_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_title: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="(untitled)")
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    topics_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
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
    ranking_snapshots: Mapped[list["IntelEventRankingSnapshot"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="IntelEventRankingSnapshot.rank",
    )

    @property
    def canonical_key(self) -> str:
        """Compatibility alias used by earlier event pipeline callers."""

        return self.event_key

    @canonical_key.setter
    def canonical_key(self, value: str) -> None:
        self.event_key = value

    @property
    def novelty(self) -> str:
        return self.novelty_status


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


class IntelEventRankingSnapshot(Base):
    """Idempotent event-level ranking output consumed by later stages."""

    __tablename__ = "intel_event_ranking_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_key", "event_id", name="uq_intel_event_rank_snapshot_event"),
        Index("ix_intel_event_rank_snapshot_key", "snapshot_key"),
        Index("ix_intel_event_rank_snapshot_rank", "snapshot_key", "rank"),
        Index("ix_intel_event_rank_snapshot_selected", "snapshot_key", "selected"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_key: Mapped[str] = mapped_column(String(128), nullable=False, default="latest")
    event_id: Mapped[int] = mapped_column(ForeignKey("intel_events.id"), nullable=False)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("intel_runs.id"), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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

    event: Mapped[IntelEvent] = relationship(back_populates="ranking_snapshots")


# Friendly aliases for callers that use the shorter historical name.
EventRankingSnapshot = IntelEventRankingSnapshot
