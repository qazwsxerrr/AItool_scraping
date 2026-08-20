"""Strict provider-neutral contracts for the single Stage-D editorial call."""

from __future__ import annotations

import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MARKDOWN_OR_URL_RE = re.compile(r"(?:https?://|www\.|\[[^\]]+\]\([^)]*\)|`|<[^>]+>)", re.IGNORECASE)
_MARKETING_WORDS = ("重磅", "颠覆", "史上最强", "最强", "革命性")
STAGE_D_TITLE_MIN_CHARS = 8
STAGE_D_TITLE_MAX_CHARS = 60
STAGE_D_SCHEMA_VERSION = "stage_d_editorial_v3"


# Keep the reason vocabulary stable so daily audit reports remain comparable.
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
    "omitted_by_editor",
    "editorial_limit",
    "weak_evidence",
    "information_gain",
    "developer_relevance",
    "industry_relevance",
    "other",
)
STAGE_D_REASON_CODE_SET = frozenset(STAGE_D_REASON_CODES)


class StageDEditorialDecision(BaseModel):
    """One complete decision returned by the Stage-D provider."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    event_id: int = Field(gt=0)
    decision: Literal["selected", "watchlist", "omitted"]
    # Only selected rows may carry a display order. Watchlist order is assigned
    # locally so the provider cannot overlap the public selected-card order.
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

    _known_reason_codes: ClassVar[frozenset[str]] = STAGE_D_REASON_CODE_SET

    @field_validator("reason_codes")
    @classmethod
    def _normalize_reason_codes(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            code = str(raw).strip().casefold().replace(" ", "_")
            if not code or len(code) > 64 or code in result:
                continue
            # Unknown provider reason codes remain auditable; they are not
            # promoted to factual claims.
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
        if self.decision == "selected":
            if self.display_order is None:
                raise ValueError("selected decision requires display_order")
            if not self.display_title_zh:
                raise ValueError("selected decision requires display_title_zh")
            if not self.title_supporting_fields:
                raise ValueError("selected decision requires title_supporting_fields")
            if self.family_position is None:
                raise ValueError("selected decision requires family_position")
            return self

        if self.display_order is not None:
            raise ValueError(f"{self.decision} decision must not contain display_order")
        if self.title_supporting_fields and not self.display_title_zh:
            raise ValueError(f"{self.decision} title_supporting_fields require display_title_zh")
        return self


class StageDEditorialResponse(BaseModel):
    """Complete Stage-D response; parser enforces exact input-ID coverage."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[STAGE_D_SCHEMA_VERSION]
    decisions: list[StageDEditorialDecision]

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "StageDEditorialResponse":
        ids = [decision.event_id for decision in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("Stage D editorial response contains duplicate event_id")
        return self


__all__ = [
    "STAGE_D_REASON_CODES",
    "STAGE_D_REASON_CODE_SET",
    "STAGE_D_SCHEMA_VERSION",
    "STAGE_D_TITLE_MAX_CHARS",
    "STAGE_D_TITLE_MIN_CHARS",
    "StageDEditorialDecision",
    "StageDEditorialResponse",
]
