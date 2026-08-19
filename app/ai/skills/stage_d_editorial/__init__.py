"""Stage D editorial selection skill.

This is a runtime project skill, not a Codex skill.  It turns Stage-C
canonical events into an auditable daily edition without changing factual
event identity or the Stage-B analysis projection.
"""

from .client import StageDProviderCallResult, StageDEditorialClient
from .models import (
    STAGE_D_REASON_CODES,
    StageDAssessment,
    StageDAssessmentResponse,
    StageDCompositionResponse,
    StageDEditorialDecision,
)
from .parser import (
    strict_parse_stage_d_assessment,
    strict_parse_stage_d_composition,
)
from .prompts import (
    STAGE_D_ASSESSMENT_JSON_SCHEMA,
    STAGE_D_ASSESSMENT_PROMPT_VERSION,
    STAGE_D_ASSESSMENT_SYSTEM_PROMPT,
    STAGE_D_COMPOSITION_JSON_SCHEMA,
    STAGE_D_COMPOSITION_PROMPT_VERSION,
    STAGE_D_COMPOSITION_SYSTEM_PROMPT,
    STAGE_D_PROMPT_VERSION,
    STAGE_D_SYSTEM_PROMPT,
    build_stage_d_assessment_payload,
    build_stage_d_composition_payload,
)

__all__ = [
    "STAGE_D_PROMPT_VERSION",
    "STAGE_D_SYSTEM_PROMPT",
    "STAGE_D_ASSESSMENT_JSON_SCHEMA",
    "STAGE_D_ASSESSMENT_PROMPT_VERSION",
    "STAGE_D_ASSESSMENT_SYSTEM_PROMPT",
    "STAGE_D_COMPOSITION_JSON_SCHEMA",
    "STAGE_D_COMPOSITION_PROMPT_VERSION",
    "STAGE_D_COMPOSITION_SYSTEM_PROMPT",
    "STAGE_D_REASON_CODES",
    "StageDAssessment",
    "StageDAssessmentResponse",
    "StageDCompositionResponse",
    "StageDEditorialDecision",
    "StageDEditorialClient",
    "StageDProviderCallResult",
    "build_stage_d_assessment_payload",
    "build_stage_d_composition_payload",
    "strict_parse_stage_d_assessment",
    "strict_parse_stage_d_composition",
]
