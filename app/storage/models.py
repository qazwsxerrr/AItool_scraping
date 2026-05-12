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
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    fetch_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    parser_type: Mapped[str] = mapped_column(String(64), nullable=False, default="feedparser")
    source_group: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    source_subtype: Mapped[str] = mapped_column(String(64), nullable=False, default="fixed")
    quality_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    spam_risk: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requires_verification: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
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
    extracted_claim: Mapped["ExtractedClaim | None"] = relationship(back_populates="candidate_item")
    evidence_items: Mapped[list["EvidenceItem"]] = relationship(back_populates="candidate_item")
    verification_item: Mapped["VerificationItem | None"] = relationship(back_populates="candidate_item")
    entity_mentions: Mapped[list["EntityMention"]] = relationship(back_populates="candidate_item")
    claim_verification_items: Mapped[list["ClaimVerificationItem"]] = relationship(back_populates="candidate_item")
    feedback_items: Mapped[list["UserFeedback"]] = relationship(back_populates="candidate_item")


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


class ExtractedClaim(Base):
    __tablename__ = "extracted_claims"
    __table_args__ = (
        UniqueConstraint("candidate_item_id", name="uq_extracted_claims_candidate_item_id"),
        Index("ix_extracted_claims_entity_name", "entity_name"),
        Index("ix_extracted_claims_entity_type", "entity_type"),
        Index("ix_extracted_claims_confidence", "confidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    official_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    huggingface_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    producthunt_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    claims_json: Mapped[str] = mapped_column(Text, nullable=False)
    release_signal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actionable_signal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    evidence_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_searched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    candidate_item: Mapped[CandidateItem] = relationship(back_populates="extracted_claim")
    claim_verification_items: Mapped[list["ClaimVerificationItem"]] = relationship(back_populates="extracted_claim")


class ClaimVerificationItem(Base):
    __tablename__ = "claim_verification_items"
    __table_args__ = (
        UniqueConstraint("extracted_claim_id", "claim_index", name="uq_claim_verification_claim_index"),
        Index("ix_claim_verification_candidate_item_id", "candidate_item_id"),
        Index("ix_claim_verification_supports_claim", "supports_claim"),
        Index("ix_claim_verification_confidence", "confidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    extracted_claim_id: Mapped[int] = mapped_column(ForeignKey("extracted_claims.id"), nullable=False)
    claim_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    supports_claim: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    evidence_item_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_flags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_response: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    candidate_item: Mapped[CandidateItem] = relationship(back_populates="claim_verification_items")
    extracted_claim: Mapped[ExtractedClaim] = relationship(back_populates="claim_verification_items")


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    __table_args__ = (
        UniqueConstraint("candidate_item_id", "url", name="uq_evidence_items_candidate_url"),
        Index("ix_evidence_items_candidate_item_id", "candidate_item_id"),
        Index("ix_evidence_items_type", "evidence_type"),
        Index("ix_evidence_items_supports_claim", "supports_claim"),
        Index("ix_evidence_items_confidence", "confidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supports_claim: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retrieval_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    url_validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unchecked")
    fetched_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_text_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_flags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    quality_flags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    candidate_item: Mapped[CandidateItem] = relationship(back_populates="evidence_items")


class SearchCacheItem(Base):
    __tablename__ = "search_cache_items"
    __table_args__ = (
        UniqueConstraint("provider", "query_hash", name="uq_search_cache_provider_query_hash"),
        Index("ix_search_cache_provider", "provider"),
        Index("ix_search_cache_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VerificationItem(Base):
    __tablename__ = "verification_items"
    __table_args__ = (
        UniqueConstraint("candidate_item_id", name="uq_verification_items_candidate_item_id"),
        Index("ix_verification_items_final_keep", "final_keep"),
        Index("ix_verification_items_final_score", "final_score"),
        Index("ix_verification_items_level", "recommendation_level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_keep: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    freshness_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommendation_level: Mapped[str] = mapped_column(String(32), nullable=False)
    relevance_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usefulness_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credibility_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    novelty_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reproducibility_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audience_fit_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_quality_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spam_risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    risk_flags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    candidate_item: Mapped[CandidateItem] = relationship(back_populates="verification_item")
    entity_mentions: Mapped[list["EntityMention"]] = relationship(back_populates="verification_item")
    recommendation_card: Mapped["RecommendationCard | None"] = relationship(back_populates="verification_item")


class CanonicalEntity(Base):
    __tablename__ = "canonical_entities"
    __table_args__ = (
        Index("ix_canonical_entities_normalized_name", "normalized_name"),
        Index("ix_canonical_entities_github_url", "github_url"),
        Index("ix_canonical_entities_best_score", "best_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    huggingface_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    producthunt_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    best_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_recommended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_update_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    major_update_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    mentions: Mapped[list["EntityMention"]] = relationship(back_populates="entity")
    feedback_items: Mapped[list["UserFeedback"]] = relationship(back_populates="entity")
    recommendation_cards: Mapped[list["RecommendationCard"]] = relationship(back_populates="entity")


class EntityMention(Base):
    __tablename__ = "entity_mentions"
    __table_args__ = (
        UniqueConstraint("entity_id", "candidate_item_id", name="uq_entity_mentions_entity_candidate"),
        Index("ix_entity_mentions_candidate_item_id", "candidate_item_id"),
        Index("ix_entity_mentions_verification_item_id", "verification_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("canonical_entities.id"), nullable=False)
    candidate_item_id: Mapped[int] = mapped_column(ForeignKey("candidate_items.id"), nullable=False)
    verification_item_id: Mapped[int | None] = mapped_column(ForeignKey("verification_items.id"), nullable=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mention_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mention_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    entity: Mapped[CanonicalEntity] = relationship(back_populates="mentions")
    candidate_item: Mapped[CandidateItem] = relationship(back_populates="entity_mentions")
    verification_item: Mapped[VerificationItem | None] = relationship(back_populates="entity_mentions")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_run_type", "run_type"),
        Index("ix_pipeline_runs_status", "status"),
        Index("ix_pipeline_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (
        Index("ix_user_feedback_entity_id", "entity_id"),
        Index("ix_user_feedback_candidate_item_id", "candidate_item_id"),
        Index("ix_user_feedback_action", "action"),
        Index("ix_user_feedback_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("canonical_entities.id"), nullable=True)
    candidate_item_id: Mapped[int | None] = mapped_column(ForeignKey("candidate_items.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    entity: Mapped[CanonicalEntity | None] = relationship(back_populates="feedback_items")
    candidate_item: Mapped[CandidateItem | None] = relationship(back_populates="feedback_items")


class RecommendationCard(Base):
    __tablename__ = "recommendation_cards"
    __table_args__ = (
        UniqueConstraint("verification_item_id", name="uq_recommendation_cards_verification_item_id"),
        Index("ix_recommendation_cards_entity_id", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("canonical_entities.id"), nullable=True)
    verification_item_id: Mapped[int] = mapped_column(ForeignKey("verification_items.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary_cn: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_recommend: Mapped[str | None] = mapped_column(Text, nullable=True)
    how_to_try: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    entity: Mapped[CanonicalEntity | None] = relationship(back_populates="recommendation_cards")
    verification_item: Mapped[VerificationItem] = relationship(back_populates="recommendation_card")
