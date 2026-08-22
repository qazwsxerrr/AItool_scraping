"""Prompt and local function schemas for the stateful Stage-C agent."""

from __future__ import annotations

from typing import Any


STAGE_C_AGENT_PROMPT_VERSION = "stage_c_agent_v4"

STAGE_C_AGENT_INSTRUCTIONS = """
<role>
你是日报流水线的 Stage C 事件编辑 Agent。将 Stage B 已准入资讯聚合为可追溯的事件候选，识别重复、后续进展和需要核验的事实。
</role>
<working_principles>
- 先用候选、正文和近期期史工具理解事件；按事件批量保存草稿，并在完成前覆盖全部 active 候选。
- 社交媒体、媒体转述、厂商性能数字或分批开放是线索，不是直接排除事件的理由。若它们影响标题、摘要或可发布性，优先用受限 web search 核验；相近事件可合并检索。
- 搜索找到支持或矛盾事实时，用 record_verification_evidence 保存本会话返回的来源和摘录，再根据证据更新草稿。若只有部分事实得到支持，删去或收窄未证实表述，仍可保留为 candidate。
- needs_review 只用于核验后仍无法确认核心事实、来源相互冲突、搜索不可用或预算已耗尽的事件；用 risk_flags 简短说明遗留的不确定性。
- 只依据候选原文、B 分析、历史记录和允许域名中的核验证据；不伪造来源、日期、版本或网页事实，也不做最终日报选稿。
- 标题和 summary_cn 使用简洁中文。通过工具完成工作；finalize_event_drafts 若返回待处理项，继续调查并修正。
</working_principles>
""".strip()


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


LIST_CANDIDATES_SCHEMA = object_schema(
    {
        "bucket": {"type": "string", "enum": ["active", "reserve"]},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
    },
    ["bucket", "offset", "limit"],
)

LIST_DRAFTS_SCHEMA = object_schema({}, [])

READ_ITEMS_SCHEMA = object_schema(
    {"item_ids": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "integer", "minimum": 1}}},
    ["item_ids"],
)

SEARCH_CANDIDATES_SCHEMA = object_schema(
    {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "bucket": {"type": "string", "enum": ["active", "reserve", "all"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
    },
    ["query", "bucket", "limit"],
)

READ_HISTORY_SCHEMA = object_schema(
    {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    ["query", "limit"],
)

EVENT_DRAFT_PROPERTIES: dict[str, Any] = {
    "draft_key": {"type": "string", "minLength": 1, "maxLength": 120},
    "item_ids": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "integer", "minimum": 1}},
    "title": {"type": "string", "minLength": 1, "maxLength": 300},
    "summary_cn": {"type": "string", "minLength": 1, "maxLength": 600},
    "topic": {"type": "string", "minLength": 1, "maxLength": 64},
    "topics": {"type": "array", "items": {"type": "string"}},
    "keywords": {"type": "array", "items": {"type": "string"}},
    "entities": {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "type", "aliases"],
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    "novelty_status": {"type": "string", "enum": ["new", "updated", "repeat", "uncertain"]},
    "prior_event_key": {"type": ["string", "null"]},
    "review_state": {"type": "string", "enum": ["candidate", "needs_review"]},
    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    "risk_flags": {"type": "array", "items": {"type": "string"}},
}

EVENT_DRAFT_REQUIRED = [
    "draft_key", "item_ids", "title", "summary_cn", "topic", "topics", "keywords", "entities",
    "novelty_status", "prior_event_key", "review_state", "confidence", "risk_flags",
]

SAVE_DRAFTS_SCHEMA = object_schema(
    {
        "drafts": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": object_schema(EVENT_DRAFT_PROPERTIES, EVENT_DRAFT_REQUIRED),
        }
    },
    ["drafts"],
)

RECORD_EVIDENCE_SCHEMA = object_schema(
    {
        "draft_key": {"type": "string", "minLength": 1, "maxLength": 120},
        "url": {"type": "string", "minLength": 8, "maxLength": 2000},
        "title": {"type": "string", "maxLength": 500},
        "excerpt": {"type": "string", "minLength": 1, "maxLength": 2000},
        "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
    ["draft_key", "url", "title", "excerpt", "claim"],
)

MARK_UNRESOLVED_SCHEMA = object_schema(
    {
        "item_ids": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "integer", "minimum": 1}},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
    ["item_ids", "reason"],
)

FINALIZE_DRAFTS_SCHEMA = object_schema({}, [])


__all__ = [
    "FINALIZE_DRAFTS_SCHEMA",
    "LIST_CANDIDATES_SCHEMA",
    "LIST_DRAFTS_SCHEMA",
    "MARK_UNRESOLVED_SCHEMA",
    "READ_HISTORY_SCHEMA",
    "READ_ITEMS_SCHEMA",
    "RECORD_EVIDENCE_SCHEMA",
    "SAVE_DRAFTS_SCHEMA",
    "SEARCH_CANDIDATES_SCHEMA",
    "STAGE_C_AGENT_INSTRUCTIONS",
    "STAGE_C_AGENT_PROMPT_VERSION",
]
