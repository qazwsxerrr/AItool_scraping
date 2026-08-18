"""Stage D editorial selection skill.

This is a runtime project skill, not a Codex skill.  It turns Stage-C
canonical events into an auditable daily edition without changing factual
event identity or the Stage-B analysis projection.
"""

from .client import StageDEditorialClient
from .models import StageDDecision, StageDEditorialResponse
from .parser import parse_stage_d_response, strict_parse_stage_d
from .prompts import STAGE_D_PROMPT_VERSION, STAGE_D_SYSTEM_PROMPT

__all__ = [
    "STAGE_D_PROMPT_VERSION",
    "STAGE_D_SYSTEM_PROMPT",
    "StageDDecision",
    "StageDEditorialClient",
    "StageDEditorialResponse",
    "parse_stage_d_response",
    "strict_parse_stage_d",
]
