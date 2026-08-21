"""Small, Responses-only transport and function-calling runtime.

The project deliberately keeps this layer local instead of adopting a broad
agent framework: the C agent needs a narrow tool surface, durable audit rows,
and the same HTTP transport used by the rest of the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

import httpx


class SupportsPost(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any], **kwargs: Any): ...


class ResponsesProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        raw_response: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.raw_response = raw_response
        self.retryable = status_code is None or status_code == 429 or (status_code is not None and status_code >= 500)


class AgentProtocolError(RuntimeError):
    pass


class AgentBudgetExceeded(AgentProtocolError):
    pass


@dataclass(frozen=True)
class FunctionTool:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[[dict[str, Any]], Mapping[str, Any]]

    def provider_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "strict": True,
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class AgentRunResult:
    response_id: str | None
    turns: int
    tool_calls: int
    web_searches: int
    finalized: bool
    last_response: Mapping[str, Any]


class ResponsesClient:
    """A minimal OpenAI Responses transport with no legacy API styles."""

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
        self.api_url = api_url.rstrip("/") if api_url else None
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = float(timeout_seconds)
        self._http_client = http_client

    @property
    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key and self.model)

    @property
    def endpoint_url(self) -> str:
        if not self.api_url:
            raise RuntimeError("Responses API is not configured")
        return self.api_url if self.api_url.casefold().endswith("/responses") else f"{self.api_url}/responses"

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.is_configured:
            raise RuntimeError("Responses API requires AI_REVIEW_API_URL, AI_REVIEW_API_KEY, and AI_REVIEW_MODEL")
        body = {key: value for key, value in dict(payload).items() if value is not None}
        body.setdefault("model", self.model)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            if self._http_client is not None:
                try:
                    response = self._http_client.post(
                        self.endpoint_url,
                        headers=headers,
                        json=body,
                        timeout=self.timeout_seconds,
                    )
                except TypeError:
                    response = self._http_client.post(self.endpoint_url, headers=headers, json=body)
            else:
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, http2=True, trust_env=True) as client:
                    response = client.post(self.endpoint_url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise ResponsesProviderError(str(exc), error_code=exc.__class__.__name__) from exc
        except Exception as exc:
            raise ResponsesProviderError(str(exc), error_code=exc.__class__.__name__) from exc

        status_code = getattr(response, "status_code", None)
        try:
            data = response.json()
        except Exception as exc:
            raise ResponsesProviderError(
                "Responses API returned invalid JSON",
                status_code=status_code,
                error_code="invalid_json",
            ) from exc
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            try:
                raise_for_status()
            except Exception as exc:
                error = _provider_error(data)
                raise ResponsesProviderError(
                    error or str(exc),
                    status_code=status_code,
                    error_code=_provider_error_code(data),
                    raw_response=data,
                ) from exc
        if not isinstance(data, Mapping):
            raise ResponsesProviderError("Responses API returned a non-object payload", status_code=status_code, raw_response=data)
        return dict(data)

    def structured(
        self,
        *,
        instructions: str,
        input_value: Mapping[str, Any] | Sequence[Any] | str,
        schema_name: str,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        response = self.create(
            {
                "input": _initial_input(instructions, input_value),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": dict(schema),
                    }
                },
            }
        )
        return extract_json_output(response)

    def run_function_agent(
        self,
        *,
        instructions: str,
        initial_input: Mapping[str, Any] | Sequence[Any] | str,
        function_tools: Sequence[FunctionTool],
        hosted_tools: Sequence[Mapping[str, Any]] = (),
        max_turns: int,
        max_tool_calls: int,
        max_web_searches: int,
        previous_response_id: str | None = None,
        on_response: Callable[[int, Mapping[str, Any]], None] | None = None,
        on_tool: Callable[[int, Mapping[str, Any], Mapping[str, Any]], None] | None = None,
    ) -> AgentRunResult:
        if max_turns < 1 or max_tool_calls < 1:
            raise ValueError("agent budgets must be positive")
        by_name = {tool.name: tool for tool in function_tools}
        if not by_name:
            raise ValueError("agent requires at least one local function tool")

        turns = 0
        tool_calls = 0
        web_searches = 0
        response_id = previous_response_id
        next_input: Any = _initial_input(instructions, initial_input) if previous_response_id is None else []
        # Most Responses providers retain tool-call state through
        # ``previous_response_id``. Keep a complete replayable transcript as
        # well: a few OpenAI-compatible gateways accept the parameter but do
        # not persist function calls, then reject a valid output-only
        # continuation with "No tool call found". The transcript is used only
        # for that narrow, safe retry path.
        replay_input: list[dict[str, Any]] | None = (
            [dict(item) for item in next_input if isinstance(item, Mapping)]
            if previous_response_id is None
            else None
        )
        last_response: dict[str, Any] = {}
        while turns < max_turns:
            turns += 1
            provider_tools = [tool.provider_definition() for tool in function_tools]
            web_search_enabled = bool(hosted_tools) and web_searches < max_web_searches
            if web_search_enabled:
                provider_tools.extend(dict(tool) for tool in hosted_tools)
            # Responses does not inherit top-level instructions across a
            # previous_response_id chain. Re-send the immutable agent policy
            # on every turn so a tool-result continuation has the same safety
            # and stage boundaries as the opening turn.
            payload: dict[str, Any] = {
                "instructions": instructions,
                "input": next_input,
                "tools": provider_tools,
            }
            if web_search_enabled:
                # Retain the source list in the auditable raw response so C
                # can bind a later verification record to an actual search.
                payload["include"] = ["web_search_call.action.sources"]
            if response_id:
                payload["previous_response_id"] = response_id
            try:
                response = self.create(payload)
            except ResponsesProviderError as exc:
                if not _requires_function_call_replay(exc) or not replay_input:
                    raise
                # Retry exactly once without previous_response_id, replaying
                # the prior model output (including the function_call) and
                # its local function_call_output in order. This follows the
                # stateless Responses function-calling shape and keeps a
                # partially compatible gateway from blocking the C workflow.
                replay_payload = dict(payload)
                replay_payload.pop("previous_response_id", None)
                replay_payload["input"] = [dict(item) for item in replay_input]
                response = self.create(replay_payload)
            last_response = response
            response_id = _text(response.get("id")) or response_id
            if on_response is not None:
                on_response(turns, response)

            output = _output_items(response)
            if replay_input is not None:
                replay_input.extend(output)
            web_searches += sum(1 for item in output if str(item.get("type") or "") == "web_search_call")
            if web_searches > max_web_searches:
                raise AgentBudgetExceeded("C agent exhausted its hosted web-search budget")
            calls = [item for item in output if str(item.get("type") or "") == "function_call"]
            if not calls:
                # A hosted tool can complete independently and return a text
                # turn before the model makes its next local function call.
                # Continue the same Responses chain once, instead of treating
                # a successful web-search turn as a terminal agent failure.
                if not response_id:
                    raise AgentProtocolError("Responses API response did not include an id for continuation")
                next_input = [
                    {
                        "role": "user",
                        "content": "Continue the Stage C workflow using local tools. Do not answer in prose; call a tool, and finalize only after active coverage is complete.",
                    }
                ]
                if replay_input is not None:
                    replay_input.extend(next_input)
                continue
            if tool_calls + len(calls) > max_tool_calls:
                raise AgentBudgetExceeded("C agent exhausted its local tool-call budget")

            outputs: list[dict[str, Any]] = []
            finalized = False
            for call in calls:
                tool_calls += 1
                name = _text(call.get("name"))
                call_id = _text(call.get("call_id"))
                if not name or not call_id:
                    raise AgentProtocolError("Responses function_call is missing name or call_id")
                tool = by_name.get(name)
                if tool is None:
                    result: Mapping[str, Any] = {"ok": False, "error": f"unknown local tool: {name}"}
                else:
                    arguments = _arguments(call.get("arguments"))
                    try:
                        result = dict(tool.handler(arguments))
                    except Exception as exc:  # keep model-facing errors structured and recoverable
                        result = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
                if on_tool is not None:
                    on_tool(turns, call, result)
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
                finalized = finalized or bool(result.get("_finalized"))
            if finalized:
                return AgentRunResult(
                    response_id=response_id,
                    turns=turns,
                    tool_calls=tool_calls,
                    web_searches=web_searches,
                    finalized=True,
                    last_response=last_response,
                )
            if not response_id:
                raise AgentProtocolError("Responses API response did not include an id for continuation")
            next_input = outputs
            if replay_input is not None:
                replay_input.extend(outputs)
        raise AgentBudgetExceeded("C agent exhausted its turn budget")

    def verify_web_search(self, *, allowed_domains: Sequence[str]) -> dict[str, Any]:
        """Perform a real hosted-web-search capability probe without local state."""

        domains = [str(value).strip() for value in allowed_domains if str(value).strip()]
        tool: dict[str, Any] = {"type": "web_search"}
        if domains:
            tool["filters"] = {"allowed_domains": domains[:100]}
        response = self.create(
            {
                "input": [
                    {
                        "role": "system",
                        "content": "Use the web search tool once, then answer only with JSON {\"web_search_available\": true}.",
                    },
                    {"role": "user", "content": "Verify that hosted web search is available."},
                ],
                "tools": [tool],
                # This is a capability probe, not an optional research turn.
                # Require a hosted call so a provider cannot return a cached
                # JSON answer while falsely appearing web-search capable.
                "tool_choice": "required",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "web_search_capability",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["web_search_available"],
                            "properties": {"web_search_available": {"type": "boolean"}},
                        },
                    }
                },
                "include": ["web_search_call.action.sources"],
            }
        )
        if not any(item.get("type") == "web_search_call" for item in _output_items(response)):
            raise ResponsesProviderError(
                "Responses provider completed the probe without a web_search_call",
                raw_response=response,
            )
        return response


def hosted_web_search_tool(*, allowed_domains: Sequence[str]) -> dict[str, Any]:
    domains = [str(value).strip() for value in allowed_domains if str(value).strip()]
    tool: dict[str, Any] = {"type": "web_search"}
    if domains:
        tool["filters"] = {"allowed_domains": domains[:100]}
    return tool


def extract_json_output(response: Mapping[str, Any]) -> dict[str, Any]:
    text = _response_output_text(response)
    if not text:
        raise ResponsesProviderError("Responses API returned no output_text", raw_response=dict(response))
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResponsesProviderError("Responses API output is not valid JSON", raw_response=dict(response)) from exc
    if not isinstance(value, Mapping):
        raise ResponsesProviderError("Responses API JSON output must be an object", raw_response=dict(response))
    return dict(value)


def _initial_input(instructions: str, value: Mapping[str, Any] | Sequence[Any] | str) -> list[dict[str, str]]:
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": str(content)},
    ]


def _output_items(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = response.get("output")
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _response_output_text(response: Mapping[str, Any]) -> str:
    direct = _text(response.get("output_text"))
    if direct:
        return direct
    for item in _output_items(response):
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            if str(part.get("type") or "") in {"output_text", "text"}:
                text = _text(part.get("text"))
                if text:
                    return text
    return ""


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentProtocolError("function_call arguments are not valid JSON") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise AgentProtocolError("function_call arguments must be an object")


def _requires_function_call_replay(exc: ResponsesProviderError) -> bool:
    """Whether a gateway lost the function call behind previous_response_id."""

    message = str(exc).casefold()
    return (
        int(exc.status_code or 0) == 400
        and "no tool call found for function call output" in message
    )


def _provider_error(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        return _text(error.get("message"))
    return _text(payload.get("message"))


def _provider_error_code(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        return _text(error.get("code")) or _text(error.get("type"))
    return _text(payload.get("code"))


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


__all__ = [
    "AgentBudgetExceeded",
    "AgentProtocolError",
    "AgentRunResult",
    "FunctionTool",
    "ResponsesClient",
    "ResponsesProviderError",
    "SupportsPost",
    "extract_json_output",
    "hosted_web_search_tool",
]
