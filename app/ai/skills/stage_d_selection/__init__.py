"""Single-call Stage-D ordered-subset selection skill."""

from .client import (
    MIN_STAGE_D_TIMEOUT_SECONDS,
    StageDSelectionCallResult,
    StageDSelectionClient,
    StageDSelectionProviderError,
)
from .models import (
    STAGE_D_SELECTION_SCHEMA_VERSION,
    StageDSelectedEvent,
    StageDSelectionResponse,
)
from .parser import strict_parse_stage_d_selection
from .prompts import (
    STAGE_D_SELECTION_JSON_SCHEMA,
    STAGE_D_SELECTION_PROMPT_VERSION,
    STAGE_D_SELECTION_SYSTEM_PROMPT,
    STAGE_D_SELECTION_TASK,
    build_stage_d_provider_payload,
    build_stage_d_selection_input,
    preflight_stage_d_selection_schema,
)

__all__ = [
    "MIN_STAGE_D_TIMEOUT_SECONDS",
    "STAGE_D_SELECTION_JSON_SCHEMA",
    "STAGE_D_SELECTION_PROMPT_VERSION",
    "STAGE_D_SELECTION_SCHEMA_VERSION",
    "STAGE_D_SELECTION_SYSTEM_PROMPT",
    "STAGE_D_SELECTION_TASK",
    "StageDSelectedEvent",
    "StageDSelectionCallResult",
    "StageDSelectionClient",
    "StageDSelectionProviderError",
    "StageDSelectionResponse",
    "build_stage_d_provider_payload",
    "build_stage_d_selection_input",
    "preflight_stage_d_selection_schema",
    "strict_parse_stage_d_selection",
]
