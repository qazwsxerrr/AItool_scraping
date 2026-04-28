from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    fetch_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    parser_type: Mapped[str] = mapped_column(String(64), nullable=False, default="feedparser")
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    raw_items: Mapped[list["RawItem"]] = relationship(back_populates="source")


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_items_source_external_id"),
        UniqueConstraint("source_id", "link", name="uq_raw_items_source_link"),
        UniqueConstraint("content_hash", name="uq_raw_items_content_hash"),
        Index("ix_raw_items_published_at", "published_at"),
        Index("ix_raw_items_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    raw_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")

    source: Mapped[Source] = relationship(back_populates="raw_items")
    normalized_item: Mapped["NormalizedItem | None"] = relationship(back_populates="raw_item")


class NormalizedItem(Base):
    __tablename__ = "normalized_items"
    __table_args__ = (
        UniqueConstraint("raw_item_id", name="uq_normalized_items_raw_item_id"),
        UniqueConstraint("dedupe_key", name="uq_normalized_items_dedupe_key"),
        Index("ix_normalized_items_published_at", "published_at"),
        Index("ix_normalized_items_language", "language"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    raw_item: Mapped[RawItem] = relationship(back_populates="normalized_item")
    candidate_item: Mapped["CandidateItem | None"] = relationship(back_populates="normalized_item")


class CandidateItem(Base):
    __tablename__ = "candidate_items"
    __table_args__ = (
        UniqueConstraint("normalized_item_id", name="uq_candidate_items_normalized_item_id"),
        Index("ix_candidate_items_source_group", "source_group"),
        Index("ix_candidate_items_status", "status"),
        Index("ix_candidate_items_score", "candidate_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    normalized_item_id: Mapped[int] = mapped_column(ForeignKey("normalized_items.id"), nullable=False)
    source_group: Mapped[str] = mapped_column(String(64), nullable=False)
    source_subtype: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    keep_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    drop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="kept")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    normalized_item: Mapped[NormalizedItem] = relationship(back_populates="candidate_item")
    ai_review_item: Mapped["AIReviewItem | None"] = relationship(back_populates="candidate_item")


class AIReviewItem(Base):
    __tablename__ = "ai_review_items"
    __table_args__ = (
        UniqueConstraint("candidate_item_id", name="uq_ai_review_items_candidate_item_id"),
        Index("ix_ai_review_items_ai_keep", "ai_keep"),
        Index("ix_ai_review_items_ai_score", "ai_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ai_keep: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ai_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    candidate_item: Mapped[CandidateItem] = relationship(back_populates="ai_review_item")
