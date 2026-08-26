from __future__ import annotations

import json

import pytest

from app.ai.responses import FunctionTool, ResponsesClient, ResponsesProviderError, hosted_web_search_tool
from app.ai.skills.stage_d_selection import (
    STAGE_D_SELECTION_SCHEMA_VERSION,
    StageDSelectionClient,
    StageDSelectionProviderError,
)


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class _Http:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, **kwargs):
        self.calls.append({"url": url, "headers": headers, "json": json})
        payload = self.payloads.pop(0)
        return payload if callable(getattr(payload, "json", None)) else _Response(payload)


class _ErrorResponse(_Response):
    status_code = 400

    def raise_for_status(self):
        raise RuntimeError("fixture HTTP 400")


def test_responses_structured_returns_validated_json_output():
    http = _Http(
        [
            {
                "id": "response-structured",
                "output_text": '{"accepted":true}',
            }
        ]
    )
    client = ResponsesClient(api_url="https://ai.example.test", api_key="secret", model="test", http_client=http)

    result = client.structured(
        instructions="Return JSON.",
        input_value={"task": "check"},
        schema_name="structured_result",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["accepted"],
            "properties": {"accepted": {"type": "boolean"}},
        },
    )

    assert result == {"accepted": True}
    payload = http.calls[0]["json"]
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["name"] == "structured_result"


def test_responses_agent_continues_after_hosted_web_search_then_finalizes_locally():
    http = _Http(
        [
            {
                "id": "response-web",
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {"sources": [{"url": "https://example.com/proof"}]},
                    }
                ],
            },
            {
                "id": "response-final",
                "output": [
                    {
                        "type": "function_call",
                        "name": "finalize_event_drafts",
                        "call_id": "call-final",
                        "arguments": "{}",
                    }
                ],
            },
        ]
    )
    client = ResponsesClient(
        api_url="https://ai.example.test/v1",
        api_key="secret",
        model="test-model",
        http_client=http,
    )
    seen: list[dict] = []
    tool = FunctionTool(
        "finalize_event_drafts",
        "Finalize local drafts.",
        {"type": "object", "additionalProperties": False, "required": [], "properties": {}},
        lambda args: seen.append(dict(args)) or {"ok": True, "_finalized": True},
    )

    result = client.run_function_agent(
        instructions="Use tools only.",
        initial_input={"run_id": 1},
        function_tools=[tool],
        hosted_tools=[hosted_web_search_tool(allowed_domains=["example.com"])],
        max_turns=4,
        max_tool_calls=4,
        max_web_searches=1,
    )

    assert result.finalized is True
    assert result.web_searches == 1
    assert seen == [{}]
    assert len(http.calls) == 2
    assert http.calls[0]["url"].endswith("/responses")
    assert http.calls[0]["json"]["tools"][-1]["type"] == "web_search"
    assert http.calls[0]["json"]["tools"][-1]["filters"]["allowed_domains"] == ["example.com"]
    assert http.calls[1]["json"]["previous_response_id"] == "response-web"
    assert http.calls[0]["json"]["instructions"] == "Use tools only."
    assert http.calls[1]["json"]["instructions"] == "Use tools only."
    assert "Continue the Stage C workflow" in http.calls[1]["json"]["input"][0]["content"]


def test_responses_agent_returns_function_outputs_on_the_same_response_chain():
    http = _Http(
        [
            {
                "id": "response-read",
                "output": [
                    {"type": "function_call", "name": "read", "call_id": "call-read", "arguments": json.dumps({"id": 7})}
                ],
            },
            {
                "id": "response-final",
                "output": [
                    {"type": "function_call", "name": "finish", "call_id": "call-finish", "arguments": "{}"}
                ],
            },
        ]
    )
    client = ResponsesClient(api_url="https://ai.example.test", api_key="secret", model="test", http_client=http)
    result = client.run_function_agent(
        instructions="tools",
        initial_input={"task": "test"},
        function_tools=[
            FunctionTool("read", "read", {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {"id": {"type": "integer"}}}, lambda args: {"ok": True, "id": args["id"]}),
            FunctionTool("finish", "finish", {"type": "object", "additionalProperties": False, "required": [], "properties": {}}, lambda args: {"ok": True, "_finalized": True}),
        ],
        hosted_tools=[hosted_web_search_tool(allowed_domains=["example.com"])],
        max_turns=4,
        max_tool_calls=4,
        max_web_searches=0,
    )

    assert result.tool_calls == 2
    continuation = http.calls[1]["json"]
    assert continuation["previous_response_id"] == "response-read"
    assert continuation["instructions"] == "tools"
    assert continuation["input"][0]["type"] == "function_call_output"
    assert json.loads(continuation["input"][0]["output"]) == {"ok": True, "id": 7}
    assert "include" not in http.calls[0]["json"]
    assert "include" not in continuation
    assert all(tool["type"] != "web_search" for tool in http.calls[0]["json"]["tools"])


def test_stage_d_schema_failure_keeps_the_full_responses_payload_for_audit():
    raw_payload = {
        "id": "selection-invalid",
        "output_text": json.dumps(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {
                        "event_id": 999,
                        "reason_code": "invalid_candidate",
                        "reason": "返回了候选池外事件。",
                    }
                ],
            },
            ensure_ascii=False,
        ),
    }
    client = StageDSelectionClient(
        api_url="https://ai.example.test",
        api_key="secret",
        model="test",
        max_retries=0,
        http_client=_Http([raw_payload]),
    )

    with pytest.raises(StageDSelectionProviderError) as raised:
        client.select([{"event_id": 1, "title": "候选事件"}], max_selected=1)

    assert raised.value.error_code == "schema_validation_failed"
    assert raised.value.raw_response == raw_payload
    assert client.last_raw_response == raw_payload


def test_stage_d_schema_failure_retries_with_missing_candidate_feedback():
    invalid_payload = {
        "id": "selection-invalid",
        "output_text": json.dumps(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {
                        "event_id": 1,
                        "reason_code": "daily_value",
                        "reason": "事件 1 入选。",
                    }
                ],
                "unselected": [],
            },
            ensure_ascii=False,
        ),
    }
    repaired_payload = {
        "id": "selection-repaired",
        "output_text": json.dumps(
            {
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "selected": [
                    {
                        "event_id": 1,
                        "reason_code": "daily_value",
                        "reason": "事件 1 入选。",
                    }
                ],
                "unselected": [
                    {
                        "event_id": 2,
                        "reason_code": "lower_editorial_value",
                        "reason": "事件 2 不进入最终子集。",
                    }
                ],
            },
            ensure_ascii=False,
        ),
    }
    http = _Http([invalid_payload, repaired_payload])
    client = StageDSelectionClient(
        api_url="https://ai.example.test",
        api_key="secret",
        model="test",
        max_retries=1,
        http_client=http,
    )

    result = client.select(
        [
            {"event_id": 1, "title": "候选事件一"},
            {"event_id": 2, "title": "候选事件二"},
        ],
        max_selected=1,
    )

    assert [row.event_id for row in result.parsed.selected] == [1]
    assert [row.event_id for row in result.parsed.unselected] == [2]
    assert result.request_metadata["provider_attempts"] == 2
    assert result.request_metadata["schema_repair_attempts"] == 1
    repair_message = http.calls[1]["json"]["input"][-1]
    assert repair_message["role"] == "user"
    feedback = json.loads(repair_message["content"])
    assert feedback["candidate_event_ids"] == [1, 2]
    assert "did not return decisions" in feedback["validation_error"]


def test_web_search_capability_probe_requires_an_actual_hosted_search_call():
    raw_payload = {"id": "probe-without-search", "output_text": '{"web_search_available":true}'}
    client = ResponsesClient(
        api_url="https://ai.example.test",
        api_key="secret",
        model="test",
        http_client=_Http([raw_payload]),
    )

    with pytest.raises(ResponsesProviderError, match="without a web_search_call") as raised:
        client.verify_web_search(allowed_domains=["example.com"])

    assert raised.value.raw_response == raw_payload


def test_responses_agent_replays_function_calls_for_a_gateway_without_response_state():
    http = _Http(
        [
            {
                "id": "response-read",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc-read",
                        "name": "read",
                        "call_id": "call-read",
                        "arguments": '{"id":7}',
                    }
                ],
            },
            _ErrorResponse(
                {
                    "error": {
                        "message": "No tool call found for function call output with call_id call-read"
                    }
                }
            ),
            {
                "id": "response-final",
                "output": [
                    {
                        "type": "function_call",
                        "name": "finish",
                        "call_id": "call-finish",
                        "arguments": "{}",
                    }
                ],
            },
        ]
    )
    client = ResponsesClient(api_url="https://ai.example.test", api_key="secret", model="test", http_client=http)

    result = client.run_function_agent(
        instructions="tools",
        initial_input={"task": "test"},
        function_tools=[
            FunctionTool(
                "read",
                "read",
                {"type": "object", "additionalProperties": False, "required": ["id"], "properties": {"id": {"type": "integer"}}},
                lambda args: {"ok": True, "id": args["id"]},
            ),
            FunctionTool(
                "finish",
                "finish",
                {"type": "object", "additionalProperties": False, "required": [], "properties": {}},
                lambda args: {"ok": True, "_finalized": True},
            ),
        ],
        max_turns=4,
        max_tool_calls=4,
        max_web_searches=0,
    )

    assert result.finalized is True
    assert len(http.calls) == 3
    assert http.calls[1]["json"]["previous_response_id"] == "response-read"
    replay = http.calls[2]["json"]
    assert "previous_response_id" not in replay
    types = [item.get("type") for item in replay["input"] if isinstance(item, dict)]
    assert types[-2:] == ["function_call", "function_call_output"]
