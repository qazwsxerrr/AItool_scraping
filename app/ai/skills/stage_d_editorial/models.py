"""Strict, provider-neutral Stage D editorial response contracts."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MARKDOWN_OR_URL_RE = re.compile(r"(?:https?://|www\.|\[[^\]]+\]\([^)]*\)|`|<[^>]+>)", re.IGNORECASE)
_MARKETING_WORDS = ("重磅", "颠覆", "史上最强", "最强", "革命性")
STAGE_D_TITLE_MIN_CHARS = 8
STAGE_D_TITLE_MAX_CHARS = 60


class StageDDecision(BaseModel):
    """One required editorial decision for one canonical event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    event_id: int = Field(gt=0)
    decision: Literal["selected", "omitted"]
    display_order: int | None = Field(default=None, ge=1)
    editorial_score: int = Field(ge=0, le=100)
    story_family_id: str = Field(min_length=1, max_length=80)
    family_position: int | None = Field(default=None, ge=1, le=2)
    display_title_zh: str | None = Field(
        default=None,
        min_length=STAGE_D_TITLE_MIN_CHARS,
        max_length=STAGE_D_TITLE_MAX_CHARS,
    )
    title_supporting_fields: list[Literal["title", "summary_cn"]] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list, max_length=12)
    editorial_reason: str = Field(min_length=1, max_length=240)
    confidence: int = Field(ge=0, le=100)

    @field_validator("reason_codes")
    @classmethod
    def _normalize_reason_codes(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            code = str(raw).strip().casefold().replace(" ", "_")
            if not code or len(code) > 64 or code in result:
                continue
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
    def _validate_decision_shape(self) -> "StageDDecision":
        if self.decision == "selected":
            if self.display_order is None:
                raise ValueError("selected decision requires display_order")
            if self.family_position is None:
                raise ValueError("selected decision requires family_position")
            if not self.display_title_zh:
                raise ValueError("selected decision requires display_title_zh")
            if not self.title_supporting_fields:
                raise ValueError("selected decision requires title_supporting_fields")
        elif any((self.display_order is not None, self.family_position is not None, self.display_title_zh is not None, bool(self.title_supporting_fields))):
            raise ValueError("omitted decision must not contain display fields")
        return self


class StageDEditorialResponse(BaseModel):
    """Full all-events Stage D response before caller-specific ID validation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["stage_d_editorial_v1"]
    decisions: list[StageDDecision]

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "StageDEditorialResponse":
        ids = [decision.event_id for decision in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("Stage D response contains duplicate event_id")
        return self
