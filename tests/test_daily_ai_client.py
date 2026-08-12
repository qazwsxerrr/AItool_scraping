from __future__ import annotations

import json

from app.ai import ClusterDecision, EventEditorialResponse, TriageResponse
from app.ai.client import ItemAnalysisClient


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, **kwargs):
        self.calls.append({"url": url, "headers": headers, "json": json, "kwargs": kwargs})
        return self.responses.pop(0)


def test_all_daily_stages_share_provider_config_but_select_stage_models():
    http = FakeHttpClient(
        [
            FakeResponse(
                {
                    "keep": True,
                    "section": "research",
                    "event_type": "paper",
                    "event_hint": "研究结果",
                    "entities": ["Paper"],
                    "impact_score": 80,
                    "novelty_score": 80,
                    "readability_score": 60,
                    "reason": "材料完整",
                    "confidence": 90,
                }
            ),
            FakeResponse({"decision": "merge", "confidence": 84, "reason": "同一事件", "canonical_event_hint": "paper-1"}),
            FakeResponse(
                {
                    "title": "研究结果",
                    "summary_cn": "摘要",
                    "why_it_matters": "便于跟进",
                    "facts": [{"text": "论文已发布", "evidence_ids": ["ev-1"]}],
                    "risk_notes": [],
                    "uncertainties": [],
                    "tags": ["research"],
                }
            ),
        ]
    )
    client = ItemAnalysisClient(
        api_url="https://api.example.test/v1",
        api_key="key",
        model="review",
        api_style="openai_chat",
        triage_model="triage",
        cluster_model="cluster",
        compose_model="compose",
        http_client=http,
    )

    triage = client.triage_item({"title": "Paper"})
    cluster = client.judge_cluster({"left": "a", "right": "b"})
    editorial = client.write_event({"event_id": "evt-1"}, [{"id": "ev-1"}])

    assert isinstance(triage.parsed, TriageResponse)
    assert isinstance(cluster.parsed, ClusterDecision)
    assert isinstance(editorial.parsed, EventEditorialResponse)
    assert [r.status for r in (triage, cluster, editorial)] == ["success"] * 3
    assert [call["json"]["model"] for call in http.calls] == ["triage", "cluster", "compose"]
    assert all(call["url"].endswith("/chat/completions") for call in http.calls)


def test_event_writer_does_not_allow_empty_facts_or_unreferenced_claims():
    empty_fact = {
        "title": "事件标题",
        "summary_cn": "摘要",
        "why_it_matters": "影响",
        "facts": [{"text": "无引用事实", "evidence_ids": []}],
    }
    http = FakeHttpClient([FakeResponse(empty_fact)])
    client = ItemAnalysisClient(api_url="https://api.example.test", api_key="key", http_client=http)

    result = client.write_event({"event_id": "evt-1"}, [{"id": "ev-1"}])

    assert result.status == "parse_error"
    assert result.raw_response == empty_fact
    assert "evidence" in (result.error or "").lower()


def test_generic_stage_payload_contains_schema_and_prompt_policy():
    http = FakeHttpClient(
        [
            FakeResponse(
                {
                    "decision": "uncertain",
                    "confidence": 40,
                    "reason": "信息不足",
                    "canonical_event_hint": None,
                }
            )
        ]
    )
    client = ItemAnalysisClient(api_url="https://api.example.test", api_key="key", http_client=http)

    result = client.judge_cluster({"left": "a", "right": "b"})
    payload = http.calls[0]["json"]

    assert result.ok is True
    assert payload["task"] == "ai_judge_cluster"
    assert payload["response_schema"]["decision"] == "merge|related|separate|uncertain"
    assert "publication gate" in payload["instructions"]
