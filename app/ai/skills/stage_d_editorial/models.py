"""Strict provider-neutral contracts for the two Stage D AI phases."""

from __future__ import annotations

import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MARKDOWN_OR_URL_RE = re.compile(r"(?:https?://|www\.|\[[^\]]+\]\([^)]*\)|`|<[^>]+>)", re.IGNORECASE)
_MARKETING_WORDS = ("重磅", "颠覆", "史上最强", "最强", "革命性")
STAGE_D_TITLE_MIN_CHARS = 8
STAGE_D_TITLE_MAX_CHARS = 60


# Stable reason vocabulary shared by D1 and D3.  Keeping the vocabulary in
# code (rather than accepting arbitrary provider text) makes audit reports
# comparable across models and runs.
STAGE_D_REASON_CODES: tuple[str, ...] = (
    "material_change",
    "impact",
    "reader_value",
    "actionability",
    "source_support",
    "freshness",
    "high_impact",
    "high_reader_value",
    "actionable",
    "fresh_information",
    "official_release",
    "first_party_update",
    "product_update",
    "model_release",
    "pricing_change",
    "funding_or_acquisition",
    "security_or_policy",
    "research_update",
    "tutorial_value",
    "community_signal",
    "recent_repeat_without_material_update",
    "same_story_no_increment",
    "low_impact",
    "low_novelty",
    "weak_specificity",
    "weak_source_support",
    "marketing_content",
    "not_shortlisted",
    "composition_limit",
    "weak_evidence",
    "information_gain",
    "developer_relevance",
    "industry_relevance",
    "other",
)
STAGE_D_REASON_CODE_SET = frozenset(STAGE_D_REASON_CODES)


class StageDAssessment(BaseModel):
    """D1's independent editorial-value assessment for one event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    event_id: int = Field(gt=0)
    material_change: int = Field(ge=0, le=100)
    impact: int = Field(ge=0, le=100)
    reader_value: int = Field(ge=0, le=100)
    actionability: int = Field(ge=0, le=100)
    source_support: int = Field(ge=0, le=100)
    freshness: int = Field(ge=0, le=100)
    must_consider: bool
    reason_codes: list[str] = Field(min_length=1, max_length=12)
    assessment_reason: str = Field(min_length=1, max_length=240)
    confidence: int = Field(ge=0, le=100)

    _known_reason_codes: ClassVar[frozenset[str]] = STAGE_D_REASON_CODE_SET

    @field_validator("reason_codes")
    @classmethod
    def _normalize_reason_codes(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            code = str(raw).strip().casefold().replace(" ", "_")
            if not code:
                raise ValueError("reason_codes must not contain empty values")
            if code not in cls._known_reason_codes:
                raise ValueError(f"unknown Stage D reason_code: {code}")
            if code in result:
                raise ValueError(f"duplicate Stage D reason_code: {code}")
            result.append(code)
        return result


class StageDAssessmentResponse(BaseModel):
    """Complete D1 response; ID coverage is checked by the parser."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["stage_d_assessment_v1"]
    assessments: list[StageDAssessment]

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "StageDAssessmentResponse":
        ids = [assessment.event_id for assessment in self.assessments]
        if len(ids) != len(set(ids)):
            raise ValueError("Stage D assessment response contains duplicate event_id")
        return self


class StageDEditorialDecision(BaseModel):
    """D3's composition decision for one short-listed event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    event_id: int = Field(gt=0)
    decision: Literal["selected", "watchlist", "omitted"]
    display_order: int | None = Field(default=None, ge=1)
    editorial_score: int = Field(default=0, ge=0, le=100)
    story_family_id: str = Field(min_length=1, max_length=80)
    family_position: int | None = Field(default=None, ge=1, le=2)
    display_title_zh: str | None = Field(
        default=None,
        min_length=STAGE_D_TITLE_MIN_CHARS,
        max_length=STAGE_D_TITLE_MAX_CHARS,
    )
    title_supporting_fields: list[Literal["title", "summary_cn"]] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list, max_length=12)
    editorial_reason: str = Field(default="", max_length=240)
    confidence: int = Field(default=0, ge=0, le=100)

    @field_validator("reason_codes")
    @classmethod
    def _normalize_reason_codes(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            code = str(raw).strip().casefold().replace(" ", "_")
            if not code or len(code) > 64 or code in result:
                continue
            # D3 may retain a provider-specific reason for forward
            # compatibility; canonical codes are still normalized.  Unknown
            # values are not silently promoted to facts and remain auditable.
            result.append(code)
        return result

    @field_validator("display_title_zh")
    @classmethod
    def _validate_display_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            return None
        if "\n" in title or "\r" in title:
            raise ValueError("display_title_zh must not contain line breaks")
        if not STAGE_D_TITLE_MIN_CHARS <= len(title) <= STAGE_D_TITLE_MAX_CHARS:
            raise ValueError(
                f"display_title_zh must be {STAGE_D_TITLE_MIN_CHARS} to {STAGE_D_TITLE_MAX_CHARS} characters"
            )
        if _MARKDOWN_OR_URL_RE.search(title):
            raise ValueError("display_title_zh must not contain Markdown or URLs")
        if any(word in title for word in _MARKETING_WORDS):
            raise ValueError("display_title_zh contains prohibited marketing language")
        return title

    @model_validator(mode="after")
    def _validate_decision_shape(self) -> "StageDEditorialDecision":
        if self.decision in {"selected", "watchlist"}:
            if self.display_order is None:
                raise ValueError(f"{self.decision} decision requires display_order")
            if not self.display_title_zh:
                raise ValueError(f"{self.decision} decision requires display_title_zh")
            if not self.title_supporting_fields:
                raise ValueError(f"{self.decision} decision requires title_supporting_fields")
        else:
            if any(
                (
                    self.display_order is not None,
                    self.family_position is not None,
                    self.display_title_zh is not None,
                    bool(self.title_supporting_fields),
                )
            ):
                raise ValueError("omitted decision must not contain display fields")
        return self


class StageDCompositionResponse(BaseModel):
    """Complete D3 response for the short-list supplied to the provider."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["stage_d_editorial_v2"]
    decisions: list[StageDEditorialDecision]

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "StageDCompositionResponse":
        ids = [decision.event_id for decision in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("Stage D composition response contains duplicate event_id")
        return self


__all__ = [
    "STAGE_D_REASON_CODES",
    "STAGE_D_REASON_CODE_SET",
    "STAGE_D_TITLE_MAX_CHARS",
    "STAGE_D_TITLE_MIN_CHARS",
    "StageDAssessment",
    "StageDAssessmentResponse",
    "StageDCompositionResponse",
    "StageDEditorialDecision",
]
