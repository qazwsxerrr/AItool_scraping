"""Minimal structured contract for one complete Stage-C aggregation call."""

from __future__ import annotations

from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ai.skills.intel_triage.parser import unwrap_provider_response


STAGE_C_SCHEMA_VERSION = "stage_c_story_aggregation_v1"


class StageCStoryMember(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item_id: int = Field(gt=0)
    relation: Literal["primary", "duplicate", "related"]


class StageCStoryCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    title_zh: str = Field(min_length=1)
    summary_zh: str = Field(min_length=1)
    primary_item_id: int = Field(gt=0)
    members: list[StageCStoryMember] = Field(min_length=1)
    novelty_status: Literal["new", "repeat", "updated"]
    prior_event_key: str | None

    @model_validator(mode="after")
    def _validate_cluster(self) -> "StageCStoryCluster":
        member_ids = [member.item_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("Stage C cluster contains duplicate item_id")
        primary_ids = [member.item_id for member in self.members if member.relation == "primary"]
        if primary_ids != [self.primary_item_id]:
            raise ValueError("Stage C cluster must contain exactly one primary matching primary_item_id")
        if self.novelty_status == "new" and self.prior_event_key is not None:
            raise ValueError("new Stage C cluster must not reference prior_event_key")
        if self.novelty_status != "new" and not self.prior_event_key:
            raise ValueError("repeat/updated Stage C cluster requires prior_event_key")
        return self


class StageCAggregationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[STAGE_C_SCHEMA_VERSION]
    clusters: list[StageCStoryCluster]

    @model_validator(mode="after")
    def _validate_global_uniqueness(self) -> "StageCAggregationResponse":
        item_ids = [member.item_id for cluster in self.clusters for member in cluster.members]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Stage C response assigns one item_id to multiple clusters")
        prior_keys = [cluster.prior_event_key for cluster in self.clusters if cluster.prior_event_key]
        if len(prior_keys) != len(set(prior_keys)):
            raise ValueError("Stage C response assigns one prior_event_key to multiple clusters")
        return self


def strict_parse_stage_c_aggregation(
    data: Any,
    *,
    item_ids: Iterable[int],
    prior_event_keys: Iterable[str],
) -> StageCAggregationResponse:
    """Parse the AI response and require exact coverage of the supplied IDs."""

    result_data, _raw = unwrap_provider_response(data)
    parsed = StageCAggregationResponse.model_validate(result_data)
    expected = [int(item_id) for item_id in item_ids]
    if len(expected) != len(set(expected)):
        raise ValueError("Stage C input contains duplicate item_id")
    actual = [member.item_id for cluster in parsed.clusters for member in cluster.members]
    unknown = sorted(set(actual) - set(expected))
    missing = sorted(set(expected) - set(actual))
    if unknown:
        raise ValueError(f"Stage C response contains unknown item_ids: {unknown}")
    if missing:
        raise ValueError(f"Stage C response is missing item_ids: {missing}")
    if len(actual) != len(expected):
        raise ValueError("Stage C response contains duplicate item_id")

    allowed_history = {str(key) for key in prior_event_keys if str(key).strip()}
    invalid_history = sorted(
        {
            str(cluster.prior_event_key)
            for cluster in parsed.clusters
            if cluster.prior_event_key and cluster.prior_event_key not in allowed_history
        }
    )
    if invalid_history:
        raise ValueError(f"Stage C response contains unknown prior_event_key: {invalid_history}")
    return parsed


__all__ = [
    "STAGE_C_SCHEMA_VERSION",
    "StageCAggregationResponse",
    "StageCStoryCluster",
    "StageCStoryMember",
    "strict_parse_stage_c_aggregation",
]
