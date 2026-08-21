"""Stateful Responses function-calling agent for Stage-C event aggregation."""

from .client import StageCAgentClient
from .prompts import STAGE_C_AGENT_INSTRUCTIONS, STAGE_C_AGENT_PROMPT_VERSION

__all__ = ["STAGE_C_AGENT_INSTRUCTIONS", "STAGE_C_AGENT_PROMPT_VERSION", "StageCAgentClient"]
