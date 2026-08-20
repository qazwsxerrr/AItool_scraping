"""Single-call Stage-D editorial selection skill."""

from .client import StageDProviderCallResult, StageDEditorialClient, StageDEditorialProviderError
from .models import (
    STAGE_D_REASON_CODES,
    STAGE_D_REASON_CODE_SET,
    STAGE_D_SCHEMA_VERSION,
    StageDEditorialDecision,
    StageDEditorialResponse,
)
from .parser import strict_parse_stage_d_editorial
from .prompts import (
    STAGE_D_JSON_SCHEMA,
    STAGE_D_PROMPT_VERSION,
    STAGE_D_SYSTEM_PROMPT,
    STAGE_D_TASK,
    build_stage_d_provider_payload,
    preflight_stage_d_schema,
)

__all__ = [
    "STAGE_D_JSON_SCHEMA",
    "STAGE_D_PROMPT_VERSION",
    "STAGE_D_REASON_CODES",
    "STAGE_D_REASON_CODE_SET",
    "STAGE_D_SCHEMA_VERSION",
    "STAGE_D_SYSTEM_PROMPT",
    "STAGE_D_TASK",
    "StageDEditorialClient",
    "StageDEditorialDecision",
    "StageDEditorialProviderError",
    "StageDEditorialResponse",
    "StageDProviderCallResult",
    "build_stage_d_provider_payload",
    "preflight_stage_d_schema",
    "strict_parse_stage_d_editorial",
]
