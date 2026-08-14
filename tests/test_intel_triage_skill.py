from __future__ import annotations

import json

import pytest

from app.ai import ItemAnalysisClient
from app.ai.client import IntelTriageClient
from app.ai.skills.intel_triage import (
    INTEL_TOPICS,
    RawIntelEnvelope,
    TriageResult,
    build_provider_payload,
    normalize_html,
    normalize_text,
    parse_triage_result,
    run_triage_batch,
)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, **kwargs):
        self.calls.append({"url": url, "headers": headers, "json": json, "kwargs": kwargs})
        return self.response


def envelope(**overrides) -> RawIntelEnvelope:
    values = {
        "item_id": 7,
        "source_id": "github_search",
        "source_group": "github_search",
        "source_content_class": "project_tool",
        "title": "Example MCP project",
        "url": "https://example.test/project/?utm_source=rss",
        "raw_html": "<p>A <b>reusable</b> MCP server.</p><script>ignore()</script>",
    }
    values.update(overrides)
    return RawIntelEnvelope(**values)


def triage_payload(**overrides):
    value = {
        "keep": True,
        "topic": "project",
        "summary_cn": "一个可复用的 MCP 项目。",
        "keywords": ["MCP", "开源"],
        "selection_score": 87,
        "scores": {"relevance": 90, "impact": 80, "total": 87},
        "novelty": "unknown",
        "paper_support": {"is_paper": False},
        "risk_flags": [],
        "confidence": 91,
    }
    value.update(overrides)
    return value


def test_raw_envelope_normalizes_html_and_aliases():
    item = envelope(item_id=12, link="https://example.test/project/", content="<p>Hello&nbsp;world</p>", content_class="project_tool")
    assert item.item_id == 12
    assert item.url == "https://example.test/project"
    assert item.body_text == "Hello world"
    assert "script" not in normalize_html(item.raw_html or "")


def test_html_fragment_normalization_is_one_way_for_fixture_like_literals():
    fixture_like = "<article><p>Use &lt; foo&gt; and version 1.2.</p><p>Next line</p></article>"
    normalized = normalize_html(fixture_like)
    assert normalized == "Use < foo> and version 1.2.\n\nNext line"
    assert normalize_text(fixture_like) == normalized
    assert normalize_html(normalized) == normalized


def test_strict_parse_has_seven_topics_and_preserves_raw_audit():
    item = envelope()
    result = parse_triage_result(triage_payload(topic="项目"), envelope=item)
    assert result.topic == "project"
    assert result.topics == ["project"]
    assert tuple(INTEL_TOPICS) == ("model", "product", "project", "industry", "tutorial", "opinion", "paper")
    assert result.raw_response and result.raw_response["selection_score"] == 87
    assert result.content_class == "project_tool"


def test_paper_arxiv_only_is_deterministically_rejected():
    item = envelope(
        source_content_class="community_social",
        url="https://arxiv.org/abs/1234",
        title="A paper",
    )
    result = parse_triage_result(
        triage_payload(
            keep=True,
            topic="paper",
            paper_support={"is_paper": True, "paper_url": item.url},
        ),
        envelope=item,
    )
    assert result.keep is False
    assert "paper:arxiv_only" in result.risk_flags


def test_provider_payloads_and_client_one_call():
    item = envelope()
    http = FakeHttp(FakeResponse(triage_payload()))
    client = IntelTriageClient(
        api_url="https://api.example.test/v1",
        api_key="key",
        model="triage-model",
        api_style="openai_chat",
        http_client=http,
    )
    result = client.triage(item)
    assert result.keep is True
    assert http.calls[0]["url"].endswith("/chat/completions")
    assert http.calls[0]["json"]["response_format"]["type"] == "json_schema"
    assert build_provider_payload(item, api_style="generic_json")["task"] == "intel_triage"

    legacy_adapter = ItemAnalysisClient(
        api_url="https://api.example.test/v1",
        api_key="key",
        model="triage-model",
        api_style="openai_chat",
        http_client=FakeHttp(FakeResponse(triage_payload())),
    )
    assert legacy_adapter.triage(item).topic == "project"


def test_batch_isolates_provider_failures():
    class Failing:
        def triage(self, item):
            if item.item_id == 2:
                raise TimeoutError("provider timeout")
            return triage_payload()

    results = run_triage_batch(Failing(), [envelope(item_id=1), envelope(item_id=2)])
    assert [result.status for result in results] == ["success", "ai_failed"]
    assert results[1].keep is False
    assert results[1].error_code == "TimeoutError"


def test_strict_parser_rejects_missing_contract_fields():
    with pytest.raises(ValueError, match="paper_support"):
        parse_triage_result({"keep": True, "topic": "project", "summary_cn": "x", "keywords": [], "selection_score": 1, "novelty": "unknown", "risk_flags": []})
