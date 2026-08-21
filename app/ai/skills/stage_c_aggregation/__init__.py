"""Bounded-call Stage-C story aggregation skill."""

from .client import (
    StageCAggregationCallResult,
    StageCAggregationClient,
    StageCAggregationProviderError,
)
from .models import (
    STAGE_C_SCHEMA_VERSION,
    StageCAggregationResponse,
    StageCStoryCluster,
    strict_parse_stage_c_aggregation,
)
from .prompts import (
    STAGE_C_JSON_SCHEMA,
    STAGE_C_PROMPT_VERSION,
    STAGE_C_SYSTEM_PROMPT,
    STAGE_C_TASK,
    build_stage_c_provider_payload,
)

__all__ = [
    "STAGE_C_JSON_SCHEMA",
    "STAGE_C_PROMPT_VERSION",
    "STAGE_C_SCHEMA_VERSION",
    "STAGE_C_SYSTEM_PROMPT",
    "STAGE_C_TASK",
    "StageCAggregationCallResult",
    "StageCAggregationClient",
    "StageCAggregationProviderError",
    "StageCAggregationResponse",
    "StageCStoryCluster",
    "build_stage_c_provider_payload",
    "strict_parse_stage_c_aggregation",
]
