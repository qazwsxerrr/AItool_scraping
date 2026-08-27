"""Small, Responses-only transport and function-calling runtime.

The project deliberately keeps this layer local instead of adopting a broad
agent framework: the C agent needs a narrow tool surface, durable audit rows,
and the same HTTP transport used by the rest of the pipeline.
"""

from __future__ import annotations

import json
import re
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


class StructuredOutputError(ResponsesProviderError):
    """A Responses provider exhausted the safe structured-output modes."""


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


@dataclass(frozen=True)
class StructuredCallResult:
    value: Any
    raw_response: Mapping[str, Any]
    mode: str
    attempts: int
    validation_failures: int


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

    def create_structured(
        self,
        *,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
        validate: Callable[[Mapping[str, Any]], Any],
        normalize: Callable[[Mapping[str, Any]], tuple[dict[str, Any], Sequence[str]]] | None = None,
    ) -> StructuredCallResult:
        """Try supported output modes, accepting only locally validated JSON."""

        modes = ("json_schema", "json_object", "text_json")
        attempts: list[dict[str, Any]] = []
        validation_failures = 0
        repair: dict[str, Any] | None = None
        last_error: BaseException | None = None

        for mode in modes:
            request_payload = _structured_mode_payload(
                payload,
                schema=schema,
                mode=mode,
                repair=repair,
            )
            response: dict[str, Any] | None = None
            record: dict[str, Any] = {"mode": mode}
            try:
                response = self.create(request_payload)
                record["raw_response"] = response
                extracted = extract_json_output(response)
                normalized = dict(extracted)
                transformations: list[str] = []
                if normalize is not None:
                    normalized, applied = normalize(normalized)
                    transformations.extend(str(value) for value in applied)
                normalized, pruned = _normalize_json_for_schema(normalized, schema)
                transformations.extend(pruned)
                value = validate(normalized)
                if transformations:
                    record["normalizations"] = transformations
                    record["normalized_output"] = normalized
                attempts.append(record)
                return StructuredCallResult(
                    value=value,
                    raw_response=_structured_audit_response(
                        response,
                        attempts,
                        accepted_mode=mode,
                    ),
                    mode=mode,
                    attempts=len(attempts),
                    validation_failures=validation_failures,
                )
            except ResponsesProviderError as exc:
                last_error = exc
                record["error"] = _error_record(exc)
                if exc.raw_response is not None and "raw_response" not in record:
                    record["raw_response"] = exc.raw_response
                attempts.append(record)
                if response is not None:
                    validation_failures += 1
                    repair = {
                        "validation_error": str(exc)[:4000],
                        "invalid_response": _compact_json_value(response, limit=12_000),
                    }
                    continue
                if not _is_structured_mode_compatibility_error(exc):
                    raise
                repair = None
            except (TypeError, ValueError) as exc:
                last_error = exc
                validation_failures += 1
                record["error"] = {
                    "code": "schema_validation_failed",
                    "message": str(exc)[:4000],
                }
                attempts.append(record)
                repair = {
                    "validation_error": str(exc)[:4000],
                    "invalid_response": _compact_json_value(response, limit=12_000),
                }

        raw_response = _structured_audit_response(None, attempts, accepted_mode=None)
        status_code = getattr(last_error, "status_code", None)
        error_code = getattr(last_error, "error_code", None) or "schema_validation_failed"
        raise StructuredOutputError(
            f"Responses structured output failed after {len(attempts)} attempts: {last_error or 'no valid output'}",
            status_code=status_code,
            error_code=error_code,
            raw_response=raw_response,
        ) from last_error

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
        response_id: str | None = None
        transcript_input = [dict(item) for item in _initial_input(instructions, initial_input)]
        last_response: dict[str, Any] = {}
        while turns < max_turns:
            turns += 1
            provider_tools = [tool.provider_definition() for tool in function_tools]
            web_search_enabled = bool(hosted_tools) and web_searches < max_web_searches
            if web_search_enabled:
                provider_tools.extend(dict(tool) for tool in hosted_tools)
            payload: dict[str, Any] = {
                "instructions": instructions,
                "input": [dict(item) for item in transcript_input],
                "tools": provider_tools,
            }
            if web_search_enabled:
                # Retain the source list in the auditable raw response so C
                # can bind a later verification record to an actual search.
                payload["include"] = ["web_search_call.action.sources"]
            response = self.create(payload)
            last_response = response
            response_id = _text(response.get("id")) or response_id
            if on_response is not None:
                on_response(turns, response)

            output = _output_items(response)
            transcript_input.extend(output)
            web_searches += sum(1 for item in output if str(item.get("type") or "") == "web_search_call")
            if web_searches > max_web_searches:
                raise AgentBudgetExceeded("C agent exhausted its hosted web-search budget")
            calls = [item for item in output if str(item.get("type") or "") == "function_call"]
            if not calls:
                # A hosted tool can complete independently and return a text
                # turn before the model makes its next local function call.
                # Continue the agent loop instead of treating a successful
                # web-search turn as a terminal agent failure.
                transcript_input.extend([
                    {
                        "role": "user",
                        "content": "Continue the Stage C workflow using local tools. Do not answer in prose; call a tool, and finalize only after active coverage is complete.",
                    }
                ])
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
            transcript_input.extend(outputs)
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
        if "output" not in response and "output_text" not in response and response.get("object") != "response":
            return dict(response)
        raise ResponsesProviderError("Responses API returned no output_text", raw_response=dict(response))
    try:
        value = json.loads(_unwrap_json_fence(text))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ResponsesProviderError("Responses API output is not valid JSON", raw_response=dict(response)) from exc
    if not isinstance(value, Mapping):
        raise ResponsesProviderError("Responses API JSON output must be an object", raw_response=dict(response))
    return dict(value)


def _normalize_json_for_schema(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Drop schema-unknown object fields without changing business values."""

    transformations: list[str] = []

    def walk(current: Any, current_schema: Any, path: str) -> Any:
        if not isinstance(current_schema, Mapping):
            return current
        properties = current_schema.get("properties")
        if isinstance(current, Mapping) and isinstance(properties, Mapping):
            result: dict[str, Any] = {}
            for key, child in current.items():
                if key not in properties:
                    transformations.append(f"drop_extra:{path}.{key}")
                    continue
                result[str(key)] = walk(child, properties[key], f"{path}.{key}")
            return result
        items = current_schema.get("items")
        if isinstance(current, list) and isinstance(items, Mapping):
            return [walk(child, items, f"{path}[{index}]") for index, child in enumerate(current)]
        if current_schema.get("type") == "integer" and isinstance(current, str) and re.fullmatch(r"[+-]?\d+", current.strip()):
            transformations.append(f"coerce_integer:{path}")
            return int(current.strip())
        return current

    return dict(walk(dict(value), schema, "$")), transformations


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


def _structured_mode_payload(
    payload: Mapping[str, Any],
    *,
    schema: Mapping[str, Any],
    mode: str,
    repair: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = {key: value for key, value in dict(payload).items() if value is not None}
    if mode == "json_schema":
        return result
    if mode == "json_object":
        result["text"] = {"format": {"type": "json_object"}}
    else:
        result.pop("text", None)
    base_input = result.get("input")
    messages = [dict(item) for item in base_input if isinstance(item, Mapping)] if isinstance(base_input, list) else []
    instruction: dict[str, Any] = {
        "structured_output_instruction": (
            "Return exactly one complete JSON object and no prose. Preserve the requested business meaning and "
            "satisfy every required field, type, enum, range, and additionalProperties rule in json_schema."
        ),
        "json_schema": dict(schema),
    }
    if repair:
        instruction["repair_instruction"] = "The previous output failed local validation. Recreate the complete object."
        instruction.update(dict(repair))
    messages.append({"role": "user", "content": json.dumps(instruction, ensure_ascii=False, default=str)})
    result["input"] = messages
    return result


def _structured_audit_response(
    accepted_response: Mapping[str, Any] | None,
    attempts: Sequence[Mapping[str, Any]],
    *,
    accepted_mode: str | None,
) -> dict[str, Any]:
    if len(attempts) == 1 and accepted_mode == "json_schema" and "normalizations" not in attempts[0]:
        raw = attempts[0].get("raw_response")
        if isinstance(raw, Mapping):
            return dict(raw)
    if len(attempts) == 1 and accepted_mode is None:
        raw = attempts[0].get("raw_response")
        if isinstance(raw, Mapping):
            return dict(raw)
    result = dict(accepted_response or {})
    result["_structured_compat"] = {
        "accepted_mode": accepted_mode,
        "provider_attempts": len(attempts),
        "attempts": [dict(item) for item in attempts],
    }
    return result


def _error_record(exc: ResponsesProviderError) -> dict[str, Any]:
    return {
        "status_code": exc.status_code,
        "code": exc.error_code or exc.__class__.__name__,
        "message": str(exc)[:4000],
    }


def _is_structured_mode_compatibility_error(exc: ResponsesProviderError) -> bool:
    return exc.status_code in {400, 422}


def _compact_json_value(value: Any, *, limit: int) -> Any:
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else text[:limit] + "...[truncated]"


def _unwrap_json_fence(value: str) -> str:
    text = value.strip()
    match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else text


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
    "StructuredCallResult",
    "StructuredOutputError",
    "SupportsPost",
    "extract_json_output",
    "hosted_web_search_tool",
]
