"""Stage-C full-batch aggregation fixtures for the 2026-08-19 replay."""

from __future__ import annotations

from typing import Any, Iterable

import pytest

from app.ai.skills.stage_c_aggregation import STAGE_C_SCHEMA_VERSION, strict_parse_stage_c_aggregation


def _row(
    item_id: int,
    *,
    event_key: str,
    topic: str,
    title: str,
    summary: str,
    source_group: str,
    content_class: str,
    subject: str,
    action: str,
    obj: str,
    keywords: Iterable[str],
    entities: Iterable[dict[str, str]],
) -> dict[str, Any]:
    """Build a candidate shaped like the Stage-C projection, without DB rows."""

    del event_key, subject, action, obj
    source_slug = source_group.replace("_", "-")
    return {
        "id": item_id,
        "source_id": f"{source_slug}-{item_id}",
        "source_group": source_group,
        "source_role": "official" if source_group in {"x_official", "official_blog"} else "publisher",
        "content_class": content_class,
        "canonical_url": f"https://{source_slug}.example/items/{item_id}",
        "external_id": f"{source_slug}:{item_id}",
        "title": title,
        "summary_cn": summary,
        "topic": topic,
        "keywords": list(keywords),
        "entities": list(entities),
        "b1_priority": 88,
    }


_AGENTCORE_GA = "agentcore-payments:general-availability:2026-08-19"
_AGENTCORE_OPENCLAW = "agentcore-payments:openclaw-integration:2026-08-19"
_AGENTCORE_LANGCHAIN = "agentcore-payments:langchain-middleware:2026-08-19"
_GLM_RELEASE = "glm-5.3:model-release:2026-08-19"
_GLM_SECURITY_REVIEW = "glm-5.3:security-results-review:2026-08-19"
_QWEN_CLINE_RANKING = "qwen3.8:cline-ranking:2026-08-19"
_QWEN_LOCAL_RELEASE = "qwen3.8:local-release:2026-08-19"
_QWEN_REVIEW = "qwen3.8:independent-review:2026-08-19"
_CODEX_DESTRUCTIVE_ACTIONS = "codex:destructive-file-risk:update:2026-08-19"
_REPLIT_FREE_MODE = "replit:free-mode-launch:2026-08-19"
_GROK_BEDROCK = "grok-4.6:bedrock-availability:2026-08-19"


def _entities(*values: tuple[str, str]) -> list[dict[str, str]]:
    return [{"type": entity_type, "name": name} for entity_type, name in values]


# IDs 60/59/86/58, 52/53/73, etc. are the Stage-C item IDs shown in
# output/comparison/stage_c_100_items_2026-08-19.md.  The 860/101 rows are
# deliberately tiny variants used to exercise tutorial/media provenance.
DUPLICATE_TOPIC_FIXTURES: dict[str, tuple[dict[str, Any], ...]] = {
    "AgentCore payments": (
        _row(
            60,
            event_key=_AGENTCORE_GA,
            topic="product_application",
            title="Amazon Bedrock AgentCore payments is now generally available",
            summary="AWS announces AgentCore payments general availability with wallets, x402, spending controls, and observability.",
            source_group="official_blog",
            content_class="official_product",
            subject="AWS",
            action="announce",
            obj="AgentCore payments general availability",
            keywords=("AgentCore payments", "Amazon Bedrock", "general availability"),
            entities=_entities(("company", "AWS"), ("product", "AgentCore payments")),
        ),
        _row(
            59,
            event_key=_AGENTCORE_GA,
            topic="product_application",
            title="AgentCore payments is now generally available in Amazon Bedrock AgentCore",
            summary="The AWS What's New entry repeats the AgentCore payments GA announcement and its payment safety controls.",
            source_group="official_blog",
            content_class="official_product",
            subject="AWS",
            action="announce",
            obj="AgentCore payments general availability",
            keywords=("AgentCore payments", "Amazon Bedrock", "payments"),
            entities=_entities(("company", "AWS"), ("product", "AgentCore payments")),
        ),
        _row(
            860,
            event_key=_AGENTCORE_GA,
            topic="developer_ecosystem",
            title="Tutorial: start using Amazon Bedrock AgentCore payments",
            summary="An AWS tutorial explains the just-announced AgentCore payments GA controls before walking through a basic request.",
            source_group="official_blog",
            content_class="tutorial",
            subject="AWS",
            action="announce",
            obj="AgentCore payments general availability",
            keywords=("AgentCore payments", "Amazon Bedrock", "tutorial"),
            entities=_entities(("company", "AWS"), ("product", "AgentCore payments")),
        ),
        _row(
            86,
            event_key=_AGENTCORE_OPENCLAW,
            topic="developer_ecosystem",
            title="Build OpenClaw agents that transact with Amazon Bedrock AgentCore payments",
            summary="An AWS how-to integrates OpenClaw with AgentCore payments and a testnet x402 plugin.",
            source_group="official_blog",
            content_class="tutorial",
            subject="AWS",
            action="integrate",
            obj="OpenClaw with AgentCore payments",
            keywords=("AgentCore payments", "OpenClaw", "x402"),
            entities=_entities(("company", "AWS"), ("project", "OpenClaw"), ("product", "AgentCore payments")),
        ),
        _row(
            58,
            event_key=_AGENTCORE_LANGCHAIN,
            topic="product_application",
            title="AgentCore Payments middleware for LangChain agents",
            summary="LangChain ships a middleware integration with deterministic budgets, x402 signatures, and LangSmith tracing.",
            source_group="official_blog",
            content_class="official_product",
            subject="LangChain",
            action="ship",
            obj="AgentCore Payments middleware",
            keywords=("AgentCore payments", "LangChain", "middleware"),
            entities=_entities(("company", "LangChain"), ("product", "AgentCore payments")),
        ),
    ),
    "GLM-5.3": (
        _row(
            52,
            event_key=_GLM_RELEASE,
            topic="model_release",
            title="Z.ai announces GLM-5.3 model release",
            summary="Z.ai announces GLM-5.3 and reports the model's launch benchmark result.",
            source_group="x_official",
            content_class="official_model_company",
            subject="Z.ai",
            action="release",
            obj="GLM-5.3",
            keywords=("GLM-5.3", "Z.ai", "release"),
            entities=_entities(("company", "Z.ai"), ("model", "GLM-5.3")),
        ),
        _row(
            53,
            event_key=_GLM_RELEASE,
            topic="model_release",
            title="GLM-5.3 from Z.ai is live on OpenRouter",
            summary="OpenRouter reports the newly released GLM-5.3 is available through its model catalog.",
            source_group="x_official",
            content_class="official_model_company",
            subject="Z.ai",
            action="release",
            obj="GLM-5.3",
            keywords=("GLM-5.3", "OpenRouter", "release"),
            entities=_entities(("company", "Z.ai"), ("model", "GLM-5.3")),
        ),
        _row(
            73,
            event_key=_GLM_SECURITY_REVIEW,
            topic="model_release",
            title="Reading Zhipu's GLM-5.3 security results past the headline number",
            summary="A media analysis evaluates GLM-5.3's security benchmarks and cautions about methodology and planned weights.",
            source_group="tech_media",
            content_class="news_media",
            subject="GLM-5.3",
            action="evaluate",
            obj="security benchmark results",
            keywords=("GLM-5.3", "security benchmark", "analysis"),
            entities=_entities(("company", "Zhipu"), ("model", "GLM-5.3")),
        ),
    ),
    "Qwen3.8": (
        _row(
            45,
            event_key=_QWEN_CLINE_RANKING,
            topic="model_release",
            title="Qwen3.8-27B reaches number one on Cline",
            summary="Qwen reports that Qwen3.8-27B reached the top local-model position on Cline.",
            source_group="x_official",
            content_class="official_model_company",
            subject="Qwen",
            action="reach",
            obj="number one on Cline",
            keywords=("Qwen3.8-27B", "Cline", "ranking"),
            entities=_entities(("company", "Qwen"), ("model", "Qwen3.8-27B")),
        ),
        _row(
            77,
            event_key=_QWEN_LOCAL_RELEASE,
            topic="model_release",
            title="Qwen3.8-27B runs locally on a laptop",
            summary="Qwen highlights local execution and browser WebGPU support for Qwen3.8-27B.",
            source_group="x_official",
            content_class="official_model_company",
            subject="Qwen",
            action="demonstrate",
            obj="local Qwen3.8-27B execution",
            keywords=("Qwen3.8-27B", "WebGPU", "local"),
            entities=_entities(("company", "Qwen"), ("model", "Qwen3.8-27B")),
        ),
        _row(
            96,
            event_key=_QWEN_REVIEW,
            topic="model_release",
            title="Independent review: Qwen3.8-27B overthinks by default",
            summary="A media review tests Qwen3.8-27B and reports excessive default reasoning and slower local execution.",
            source_group="tech_media",
            content_class="news_media",
            subject="Qwen3.8-27B",
            action="evaluate",
            obj="default reasoning behavior",
            keywords=("Qwen3.8-27B", "review", "overthinking"),
            entities=_entities(("model", "Qwen3.8-27B")),
        ),
    ),
    "Codex destructive-file risk": (
        _row(
            47,
            event_key=_CODEX_DESTRUCTIVE_ACTIONS,
            topic="product_application",
            title="Codex reduces risk from potentially destructive actions",
            summary="The official update recaps safeguards added after Codex could delete user files.",
            source_group="x_official",
            content_class="official_product",
            subject="OpenAI",
            action="mitigate",
            obj="Codex destructive-file risk",
            keywords=("Codex", "file deletion", "safety"),
            entities=_entities(("company", "OpenAI"), ("product", "Codex")),
        ),
        _row(
            46,
            event_key=_CODEX_DESTRUCTIVE_ACTIONS,
            topic="product_application",
            title="Codex's rare file-deletion incident and layered safeguards",
            summary="A second official account describes the same Codex file-deletion risk and the resulting permission and evaluation controls.",
            source_group="x_official",
            content_class="official_product",
            subject="OpenAI",
            action="mitigate",
            obj="Codex destructive-file risk",
            keywords=("Codex", "file deletion", "safety"),
            entities=_entities(("company", "OpenAI"), ("product", "Codex")),
        ),
    ),
    "Replit Free Mode": (
        _row(
            18,
            event_key=_REPLIT_FREE_MODE,
            topic="product_application",
            title="Replit Free Mode powered by GPT-5.6 Luna",
            summary="Replit announces Free Mode, powered by OpenAI GPT-5.6 Luna.",
            source_group="x_official",
            content_class="official_product",
            subject="Replit",
            action="launch",
            obj="Free Mode powered by GPT-5.6 Luna",
            keywords=("Replit", "Free Mode", "GPT-5.6 Luna"),
            entities=_entities(("company", "Replit"), ("product", "Free Mode"), ("model", "GPT-5.6 Luna")),
        ),
        _row(
            1,
            event_key=_REPLIT_FREE_MODE,
            topic="product_application",
            title="Replit expands software creation with GPT-5.6 Luna",
            summary="OpenAI's article reports the same Replit Free Mode launch and token-free access promise.",
            source_group="official_blog",
            content_class="official_product",
            subject="Replit",
            action="launch",
            obj="Free Mode powered by GPT-5.6 Luna",
            keywords=("Replit", "Free Mode", "GPT-5.6 Luna"),
            entities=_entities(("company", "Replit"), ("product", "Free Mode"), ("model", "GPT-5.6 Luna")),
        ),
        _row(
            101,
            event_key=_REPLIT_FREE_MODE,
            topic="product_application",
            title="Media recap: Replit Free Mode opens software creation to everyone",
            summary="A media recap describes the same Free Mode launch and its GPT-5.6 Luna backing.",
            source_group="tech_media",
            content_class="news_media",
            subject="Replit",
            action="launch",
            obj="Free Mode powered by GPT-5.6 Luna",
            keywords=("Replit", "Free Mode", "GPT-5.6 Luna"),
            entities=_entities(("company", "Replit"), ("product", "Free Mode"), ("model", "GPT-5.6 Luna")),
        ),
    ),
    "Grok 4.6 on Bedrock": (
        _row(
            26,
            event_key=_GROK_BEDROCK,
            topic="model_release",
            title="Amazon Bedrock now supports Grok 4.6",
            summary="AWS announces Grok 4.6 availability on Amazon Bedrock with a 500K context window.",
            source_group="official_blog",
            content_class="official_product",
            subject="xAI",
            action="launch",
            obj="Grok 4.6 on Amazon Bedrock",
            keywords=("Grok 4.6", "Amazon Bedrock", "availability"),
            entities=_entities(("company", "xAI"), ("model", "Grok 4.6"), ("product", "Amazon Bedrock")),
        ),
        _row(
            15,
            event_key=_GROK_BEDROCK,
            topic="model_release",
            title="Grok 4.6 is live on Amazon Bedrock",
            summary="xAI confirms the same Grok 4.6 Bedrock availability announcement.",
            source_group="x_official",
            content_class="official_product",
            subject="xAI",
            action="launch",
            obj="Grok 4.6 on Amazon Bedrock",
            keywords=("Grok 4.6", "Amazon Bedrock", "availability"),
            entities=_entities(("company", "xAI"), ("model", "Grok 4.6"), ("product", "Amazon Bedrock")),
        ),
    ),
}


def _full_batch_response() -> dict[str, Any]:
    clusters: list[dict[str, Any]] = []
    for topic, rows in DUPLICATE_TOPIC_FIXTURES.items():
        clusters.append(
            {
                "title_zh": topic,
                "summary_zh": f"聚合 {topic} 的官方发布、接入、教程和相关解读。",
                "item_ids": [int(row["id"]) for row in rows],
                "novelty_status": "new",
                "prior_event_key": None,
            }
        )
    return {"schema_version": STAGE_C_SCHEMA_VERSION, "clusters": clusters}


def test_single_ai_response_can_merge_each_duplicate_topic_into_one_story():
    rows = [row for values in DUPLICATE_TOPIC_FIXTURES.values() for row in values]

    parsed = strict_parse_stage_c_aggregation(
        _full_batch_response(),
        item_ids=[int(row["id"]) for row in rows],
        prior_event_keys=[],
    )

    actual = {
        cluster.title_zh: set(cluster.item_ids)
        for cluster in parsed.clusters
    }
    expected = {
        topic: {int(row["id"]) for row in values}
        for topic, values in DUPLICATE_TOPIC_FIXTURES.items()
    }
    assert actual == expected


def test_single_ai_response_must_cover_every_input_item_once():
    rows = [row for values in DUPLICATE_TOPIC_FIXTURES.values() for row in values]
    response = _full_batch_response()
    response["clusters"][0]["item_ids"].pop()

    with pytest.raises(ValueError, match="missing item_ids"):
        strict_parse_stage_c_aggregation(
            response,
            item_ids=[int(row["id"]) for row in rows],
            prior_event_keys=[],
        )


def test_single_ai_response_rejects_duplicate_global_item_assignment():
    rows = [row for values in DUPLICATE_TOPIC_FIXTURES.values() for row in values]
    response = _full_batch_response()
    response["clusters"][1]["item_ids"].append(response["clusters"][0]["item_ids"][0])

    with pytest.raises(ValueError, match="assigns one item_id to multiple clusters"):
        strict_parse_stage_c_aggregation(
            response,
            item_ids=[int(row["id"]) for row in rows],
            prior_event_keys=[],
        )


def test_v2_contract_has_no_redundant_primary_cross_field():
    rows = [row for values in DUPLICATE_TOPIC_FIXTURES.values() for row in values]
    response = _full_batch_response()
    response["clusters"][0]["primary_item_id"] = response["clusters"][0]["item_ids"][0]

    with pytest.raises(ValueError, match="primary_item_id"):
        strict_parse_stage_c_aggregation(
            response,
            item_ids=[int(row["id"]) for row in rows],
            prior_event_keys=[],
        )
