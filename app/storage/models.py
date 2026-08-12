"""SQLAlchemy models for the compact intelligence pipeline.

The data layer intentionally contains only the entities used by the v2
``fetch -> process -> export`` flow. Historical stage tables are not migrated;
the local SQLite database may be recreated from this metadata.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    # V3 governance metadata.  These columns deliberately have safe defaults
    # so existing callers that construct ``Source`` rows directly remain
    # compatible with a freshly-created database.
    tier: Mapped[str] = mapped_column(String(8), nullable=False, default="p4")
    topic_scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    primary_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    citation_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="discovery_only")
    account_verification_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spam_risk: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requires_verification: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    content_class: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    verification_policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Conditional request and source-health state consumed by the daily jobs.
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
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_depth: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class Document(Base):
    """Bounded source snapshot retained for auditable event evidence."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_documents_canonical_url"),
        Index("ix_documents_item_id", "item_id"),
        Index("ix_documents_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("intel_items.id"), nullable=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="fetched")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class TriageReview(Base):
    """One structured triage result per intel item, including raw AI JSON."""

    __tablename__ = "triage_reviews"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_triage_reviews_item_id"),
        Index("ix_triage_reviews_section", "section"),
        Index("ix_triage_reviews_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    keep: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    impact_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    novelty_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    readability_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    claim_types_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deterministic_score_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Event(Base):
    """Canonical event selected into a daily edition."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("canonical_key", name="uq_events_canonical_key"),
        Index("ix_events_section_state", "section", "state"),
        Index("ix_events_window", "discovered_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_key: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    repository_release_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    arxiv_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    primary_item_id: Mapped[int | None] = mapped_column(ForeignKey("intel_items.id"), nullable=True)
    primary_document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    primary_source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class EventEvidence(Base):
    """Relation between an event and source item/document evidence."""

    __tablename__ = "event_evidence"
    __table_args__ = (
        UniqueConstraint("event_id", "item_id", "document_id", "role", name="uq_event_evidence_relation"),
        Index("ix_event_evidence_event", "event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("intel_items.id"), nullable=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="supplementary")
    support_level: Mapped[str] = mapped_column(String(32), nullable=False, default="supplementary")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    citation_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_types_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ClusterDecision(Base):
    """Auditable deterministic/AI judgement for a candidate item pair."""

    __tablename__ = "cluster_decisions"
    __table_args__ = (
        UniqueConstraint("pair_key", name="uq_cluster_decisions_pair_key"),
        Index("ix_cluster_decisions_decision", "decision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair_key: Mapped[str] = mapped_column(String(128), nullable=False)
    left_item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    right_item_id: Mapped[int] = mapped_column(ForeignKey("intel_items.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="uncertain")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_event_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class EventEditorialReview(Base):
    """Structured event copy and evidence references, retained verbatim."""

    __tablename__ = "event_editorial_reviews"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_editorial_reviews_event_id"),
        Index("ix_event_editorial_reviews_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_it_matters: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    risk_notes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    uncertainties_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    valid_evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DailyEdition(Base):
    """One idempotent daily composition attempt and its publication gates."""

    __tablename__ = "daily_editions"
    __table_args__ = (UniqueConstraint("edition_date", "profile_name", name="uq_daily_editions_date_profile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    profile_name: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    gate_results_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    publish_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    markdown_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    events_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DailyEventEntry(Base):
    """Ordered event snapshot belonging to one daily edition."""

    __tablename__ = "daily_event_entries"
    __table_args__ = (
        UniqueConstraint("edition_id", "event_id", name="uq_daily_event_entries_event"),
        UniqueConstraint("edition_id", "position", name="uq_daily_event_entries_position"),
        Index("ix_daily_event_entries_section", "edition_id", "section"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_id: Mapped[int] = mapped_column(ForeignKey("daily_editions.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rendered_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="selected")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
