"""Strict provider-neutral contract for Stage-D event selection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


STAGE_D_SELECTION_SCHEMA_VERSION = "stage_d_selection_v1"


class StageDSelectedEvent(BaseModel):
    """One selected Stage-C event; list order is the final display order."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    event_id: int = Field(gt=0)
    reason_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    reason: str = Field(min_length=1, max_length=240)


class StageDUnselectedEvent(BaseModel):
    """One unselected Stage-C event."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    event_id: int = Field(gt=0)
    reason_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    reason: str = Field(min_length=1, max_length=240)


class StageDSelectionResponse(BaseModel):
    """Ordered subset selected from the Stage-C candidate event pool."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[STAGE_D_SELECTION_SCHEMA_VERSION]
    selected: list[StageDSelectedEvent]
    unselected: list[StageDUnselectedEvent]

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "StageDSelectionResponse":
        event_ids = [row.event_id for row in self.selected] + [row.event_id for row in self.unselected]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("Stage D selection contains duplicate event_id")
        return self


__all__ = [
    "STAGE_D_SELECTION_SCHEMA_VERSION",
    "StageDSelectedEvent",
    "StageDUnselectedEvent",
    "StageDSelectionResponse",
]
