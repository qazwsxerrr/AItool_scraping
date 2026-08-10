"""SQLAlchemy models for the compact intelligence pipeline.

The data layer intentionally contains only the entities used by the v2
``fetch -> process -> export`` flow. Historical stage tables are not migrated;
the local SQLite database may be recreated from this metadata.
"""

from __future__ import annotations

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
    # schema remains inspectable without a compatibility/migration layer.
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
    quality_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spam_risk: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requires_verification: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    content_class: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    verification_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

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
    verified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    verification: Mapped["IntelItemVerification | None"] = relationship(back_populates="item", uselist=False)


class AIItemReview(Base):
    """At most one structured model result for each intelligence item."""

    __tablename__ = "ai_item_reviews"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_ai_item_reviews_item_id"),
        Index("ix_ai_item_reviews_status", "status"),
        Index("ix_ai_item_reviews_keep", "keep"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="item_analysis_v1")
    keep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_class: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    needs_verification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    official_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    item: Mapped[IntelItem] = relationship(back_populates="ai_review")


class IntelItemVerification(Base):
    """One lightweight verification result for an item that needs it."""

    __tablename__ = "item_verifications"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_item_verifications_item_id"),
        Index("ix_item_verifications_status", "status"),
        Index("ix_item_verifications_mode", "mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    supports_basic_fact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    item: Mapped[IntelItem] = relationship(back_populates="verification")
