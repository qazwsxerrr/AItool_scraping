"""Responses-backed runtime for the stateful Stage-C event agent."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from app.ai.responses import AgentRunResult, FunctionTool, ResponsesClient, SupportsPost
from app.config.settings import Settings

from .prompts import STAGE_C_AGENT_INSTRUCTIONS


class StageCAgentClient:
    transport = "responses"

    def __init__(
        self,
        *,
        api_url: str | None,
        api_key: str | None,
        model: str | None,
        timeout_seconds: float = 30.0,
        http_client: SupportsPost | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self._responses = ResponsesClient(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout_seconds=self.timeout_seconds,
            http_client=http_client,
        )

    @classmethod
    def from_settings(cls, settings: Settings, http_client: SupportsPost | None = None) -> "StageCAgentClient":
        return cls(
            api_url=settings.ai_review_api_url,
            api_key=settings.ai_review_api_key,
            model=settings.ai_review_model,
            timeout_seconds=max(float(settings.stage_c_timeout_seconds), 1.0),
            http_client=http_client,
        )

    @property
    def is_configured(self) -> bool:
        return self._responses.is_configured

    def run(
        self,
        *,
        initial_context: Mapping[str, Any],
        function_tools: Sequence[FunctionTool],
        max_turns: int,
        max_tool_calls: int,
        previous_response_id: str | None = None,
        on_response: Callable[[int, Mapping[str, Any]], None] | None = None,
        on_tool: Callable[[int, Mapping[str, Any], Mapping[str, Any]], None] | None = None,
    ) -> AgentRunResult:
        return self._responses.run_function_agent(
            instructions=STAGE_C_AGENT_INSTRUCTIONS,
            initial_input=initial_context,
            function_tools=function_tools,
            hosted_tools=(),
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            max_web_searches=0,
            previous_response_id=previous_response_id,
            on_response=on_response,
            on_tool=on_tool,
        )

__all__ = ["StageCAgentClient"]
