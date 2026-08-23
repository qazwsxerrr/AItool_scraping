"""Prompt and local function schemas for the stateful Stage-C agent."""

from __future__ import annotations

from typing import Any


STAGE_C_AGENT_PROMPT_VERSION = "stage_c_agent_v9"

STAGE_C_AGENT_INSTRUCTIONS = """
<role>
你是日报流水线的 Stage C 事件聚合 Agent。将 Stage B 已准入资讯组织为可追溯事件，区分同一事件、后续进展、同主题不同事件，并比较最近三个已发布日报日期中的历史事件。
</role>
<working_principles>
- 先读取候选和正文，再按具体事件聚合。共享公司、产品或主题不足以证明是同一事件；只有主体、动作、对象、版本/阶段和时间锚点整体一致时才合并。
- 对候选关系使用四类判断：same_event 可合并；follow_up 是同一事件链的后续进展但保持独立事件；same_topic 只表示主题相关；unrelated 表示无关。不要把预告、预览、正式发布、价格变化、能力更新等不同动作仅因主体相同而合并。
- 每个多成员草稿必须给出 aggregation_basis。标题和摘要只能综合成员原文中可追溯的事实，不得用常识补齐。
- 只用 read_recent_history 判断最近三期已发布日报是否报道过；网页搜索结果不得扩大历史去重窗口。
- repeat 表示历史事件中的核心事实已经报道且当前没有实质变化。updated 必须列出 material_changes，并为每项变化提供当前 supporting_item_ids。新增转载、来源、改写、评论或背景信息不构成实质变化。
- 实质变化包括生命周期状态、版本、能力、API、价格、许可、开放范围、地区、平台、开源状态、正式确认/否认或影响结论的新数据发生变化。
- novelty_status 与事件本身是否具有候选资格是两个独立判断。new 只表示最近三期正式日报中未发现同一事件，不能单独证明它可以进入候选池。
- 严格区分“归因真实性”和“事件实质性”：前者判断某项说法是否确由相关主体作出，后者判断该资讯的事件核心是否对应已经发生的外部状态变化。搜索再次找到同一说法，通常只能提高归因可信度，不能自动证明其描述的目标、效果、领先性或未来结果已经实现。
- 每个草稿先用一句话确定标题和摘要要求读者相信的事件核心，再填写 substance_status 和 substantive_facts。不要按个别词语分类；应判断完整语义：即使不把“某主体作出这项表态”本身当作事件结果，现有材料是否仍直接支持一个已经发生、可识别、可独立成稿的外部变化。
- concrete 表示事件核心已经发生，或正式公告本身已经形成明确、可追溯、可执行的结果，并至少有一项当前成员直接支持的 substantive_facts；intent_only 表示材料能够确认的核心仅是方向、愿景、目标、计划、预测、自我评价、宣传性比较或没有明确执行点的投入；uncertain 表示材料声称存在实质变化，但现有证据无法确认。
- substantive_facts 必须直接支撑同一个事件核心。材料中的历史背景、旁支产品、既有动作或其他真实细节不得用来补足另一个核心主张；若旁支事实本身值得成稿，应创建或重写为以该事实为核心的独立事件。
- 判断完整 claim 的语义，不根据 event_action、lifecycle_state、fact_type 名称或“宣布”“发布”等词语自动推断实质性。正式公告只有在形成明确外部变化时才属于 concrete，例如产品已经发布或可获取、规则已经生效或具有确定生效条件、交易已经签署、经营数据已经披露，或开放范围、价格、版本和时间节点已经明确。
- 按固定顺序映射 review_state，避免把新旧判断、搜索状态和实质性混为一谈：
  1. 已确认 repeat 且没有 material_changes：rejected；是否重复仍无法确认：novelty_status=uncertain、review_state=needs_review。
  2. 非 repeat 事件中，substance_status=concrete 且有直接证据：candidate。
  3. 材料声称存在外部变化，但关键事实缺证、冲突、搜索失败或仍无法确认，且继续核验可能改变结论：needs_review。
  4. 能确认的事件核心只有表态、意图、预测、评价、宣传，或背景与旁支事实不能支撑该核心：rejected。rejected 不表示原话为假。
- 只有列出并由当前成员直接支持实质变化时才可使用 updated 并继续判断候选资格。若材料包含另一个已发生且可独立成稿的事实，应以该事实重写或拆成独立事件，不能用它替原核心取得 candidate。
- 对聚合歧义、弱来源实质更新、来源冲突或关键事实不确定，先保存 needs_review 草稿，再调用 search_web。搜索前明确缺失的事件核心证据，query 和 claim 必须核验可能改变 review_state 的关键事实，不能只重复确认文章、转载或原话存在。搜索结果必须通过 attach_search_evidence 按 result_id 绑定到草稿和具体 claim；不得自行提供搜索结果之外的 URL。
- 搜索后必须判断证据确认的是归因真实性还是事件核心对应的外部状态变化。只有发现并绑定了能改变事件核心判断的具体事实，才可改为 concrete/candidate；只确认表态存在时不得升级。搜索后仍缺证、证据冲突、搜索不可用或预算耗尽时保留 needs_review；确认没有外部状态变化时使用 rejected。
- Stage C 生成完整审计事件池，但只有 candidate 和 needs_review 转交 Stage D；rejected 保留审计，不进入 Stage D。
- 只依据候选原文、B 分析、近三期正式日报和已绑定搜索证据；不伪造来源、日期、版本或网页事实。
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

SEARCH_WEB_SCHEMA = object_schema(
    {
        "draft_key": {"type": ["string", "null"], "maxLength": 120},
        "query": {"type": "string", "minLength": 2, "maxLength": 300},
        "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
        "topic": {"type": "string", "enum": ["general", "news"]},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    ["draft_key", "query", "claim", "topic", "max_results"],
)

ATTACH_SEARCH_EVIDENCE_SCHEMA = object_schema(
    {
        "draft_key": {"type": "string", "minLength": 1, "maxLength": 120},
        "result_id": {"type": "string", "minLength": 8, "maxLength": 64},
        "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
        "verdict": {"type": "string", "enum": ["supports", "contradicts", "contextual"]},
    },
    ["draft_key", "result_id", "claim", "verdict"],
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
    "event_action": {
        "type": "string",
        "enum": [
            "release", "launch", "update", "announce", "open_source", "pricing",
            "availability", "research", "partnership", "policy", "funding", "strategy", "other",
        ],
    },
    "lifecycle_state": {
        "type": "string",
        "enum": ["rumor", "announced", "preview", "beta", "ga", "deprecated", "not_applicable", "uncertain"],
    },
    "aggregation_basis": {
        "type": "array",
        "items": {
            "type": "string",
            "enum": [
                "exact_identity", "direct_repost", "same_subject", "same_action",
                "same_object", "same_version", "same_lifecycle", "same_time_anchor",
                "complementary_facts",
            ],
        },
    },
    "novelty_status": {"type": "string", "enum": ["new", "updated", "repeat", "uncertain"]},
    "prior_event_key": {"type": ["string", "null"]},
    "novelty_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
    "material_changes": {
        "type": "array",
        "items": object_schema(
            {
                "change_type": {
                    "type": "string",
                    "enum": [
                        "lifecycle", "version", "capability", "api", "pricing", "license",
                        "availability", "region", "platform", "open_source", "confirmation",
                        "data", "other",
                    ],
                },
                "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
                "supporting_item_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 40,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            ["change_type", "claim", "supporting_item_ids"],
        ),
    },
    "substance_status": {
        "type": "string",
        "enum": ["concrete", "intent_only", "uncertain"],
    },
    "substantive_facts": {
        "type": "array",
        "items": object_schema(
            {
                "fact_type": {
                    "type": "string",
                    "enum": [
                        "product", "model", "capability", "timeline", "commercial",
                        "organization", "policy", "research", "availability", "data", "other",
                    ],
                },
                "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
                "supporting_item_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 40,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            ["fact_type", "claim", "supporting_item_ids"],
        ),
    },
    "review_state": {"type": "string", "enum": ["candidate", "needs_review", "rejected"]},
    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    "risk_flags": {"type": "array", "items": {"type": "string"}},
}

EVENT_DRAFT_REQUIRED = [
    "draft_key", "item_ids", "title", "summary_cn", "topic", "topics", "keywords", "entities",
    "event_action", "lifecycle_state", "aggregation_basis", "novelty_status", "prior_event_key",
    "novelty_reason", "material_changes", "substance_status", "substantive_facts",
    "review_state", "confidence", "risk_flags",
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

MARK_UNRESOLVED_SCHEMA = object_schema(
    {
        "item_ids": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "integer", "minimum": 1}},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
    },
    ["item_ids", "reason"],
)

FINALIZE_DRAFTS_SCHEMA = object_schema({}, [])


__all__ = [
    "ATTACH_SEARCH_EVIDENCE_SCHEMA",
    "FINALIZE_DRAFTS_SCHEMA",
    "LIST_CANDIDATES_SCHEMA",
    "LIST_DRAFTS_SCHEMA",
    "MARK_UNRESOLVED_SCHEMA",
    "READ_HISTORY_SCHEMA",
    "READ_ITEMS_SCHEMA",
    "SAVE_DRAFTS_SCHEMA",
    "SEARCH_CANDIDATES_SCHEMA",
    "SEARCH_WEB_SCHEMA",
    "STAGE_C_AGENT_INSTRUCTIONS",
    "STAGE_C_AGENT_PROMPT_VERSION",
]
