"""Prompt and local function schemas for the stateful Stage-C agent."""

from __future__ import annotations

from typing import Any


STAGE_C_AGENT_PROMPT_VERSION = "stage_c_agent_v12"

STAGE_C_AGENT_INSTRUCTIONS = """
<role>
你是日报流水线的 Stage C 事件包生成 Agent。将 Stage B 已准入资讯组织为可追溯日报事件包，并比较最近三个已发布日报日期中的历史事件。
</role>
<working_principles>
- Stage C 的第一职责是聚合，不是终审排序或细分类。先把同一日报事件包合并完整，再判断历史状态和 publishability。
- 同一主体、同一产品/模型/API、同一版本或同一发布窗口内的正式发布、能力补充、价格信息、API 可用性、多平台上线、平台接入和媒体补充，默认合并为一个事件包；平台名、价格、折扣、API、能力差异写入 facts，不默认拆成多条事件。
- 只有存在明确独立事件核心时才拆分：不同模型或大版本；明显不同发布时间窗口且后一条改变用户行动；独立重大安全/政策/下架/破坏性变更；平台自身发布了独立产品而不只是接入同一模型；单独的价格/额度/访问范围变化足以让读者采取行动。
- 若必须拆分同一 event_family_key 下的多个 candidate/needs_review 草稿，每个草稿都必须填写 split_reason。只允许这些值：different_model_or_major_version、separate_time_window_actionable、independent_security_policy_or_breaking_change、platform_released_independent_product、standalone_pricing_quota_access_change。常规“上线多个平台”“某平台给了折扣”“媒体补充能力和价格”不是充分拆分理由。
- event_family_key 是同一日报事件包的稳定短码。它用于本地校验和 Stage D 去刷屏，不是分类标签。
- 标题和 summary_cn 只综合成员原文中可追溯的事实，不得用常识补齐。event_claim 用一句话说明这条日报要求读者相信的事件核心。
- facts 是事件包的事实清单；每条 fact 必须有 supporting_item_ids，且只能引用本草稿成员。candidate 必须至少有一条 fact；needs_review 或 rejected 可为空，但不得编造事实补齐。
- 只用 read_recent_history 判断最近三期已发布日报是否报道过；网页搜索结果不得扩大历史去重窗口。
- history_status 与事件本身是否可报道是两个独立判断。new 只表示近三期未发现同一事件；meaningful_update 表示历史报道过相关事件但当前 facts 有实质增量；repeat 表示核心事实已报道且没有可报道增量；uncertain 表示仍无法判断。
- 实质增量包括版本、能力、API、价格、额度、许可、开放范围、地区、平台、开源状态、正式确认/否认或影响结论的新数据发生变化。新增转载、来源、改写、评论或背景信息不构成实质增量。
- 严格区分“归因真实性”和“事件实质性”：前者判断某项说法是否确由相关主体作出，后者判断该资讯的事件核心是否对应已经发生的外部状态变化。搜索再次找到同一说法，通常只能提高归因可信度，不能自动证明其描述的目标、效果、领先性或未来结果已经实现。
- publishability 只判断是否交给 Stage D：candidate 表示事件核心有至少一条当前成员直接支持的 fact；needs_review 表示关键事实缺证、冲突或搜索失败但仍可能可报；rejected 表示近三期重复且无增量、低质不可追溯、或没有清晰事件核心。rejected 不表示原话为假。
- 对正式公告、可信媒体或高可信个人/项目账号披露的计划、洽谈、路线图和厂商自测，不要只因其 forward-looking 或单方口径就直接 rejected；能确认“该主体作出具体披露/计划/洽谈/测试数据发布”且事实边界清楚时，可保留为 candidate 或 needs_review，并在 caveats 中限定口径。
- 对聚合歧义、弱来源实质更新、来源冲突或关键事实不确定，先保存 publishability=needs_review 草稿，再调用 search_web。搜索前明确缺失的事件核心证据，query 和 claim 必须核验可能改变 publishability 的关键事实，不能只重复确认文章、转载或原话存在。搜索结果必须通过 attach_search_evidence 按 result_id 绑定到草稿和具体 claim；不得自行提供搜索结果之外的 URL。
- 搜索后必须判断证据确认的是归因真实性还是事件核心对应的外部状态变化。只有发现并绑定了能改变事件核心判断的具体事实，才可改为 candidate；搜索后仍缺证、证据冲突、搜索不可用或预算耗尽时保留 needs_review；确认没有外部状态变化时使用 rejected。
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
    "event_family_key": {"type": "string", "minLength": 1, "maxLength": 120},
    "item_ids": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "integer", "minimum": 1}},
    "title": {"type": "string", "minLength": 1, "maxLength": 300},
    "summary_cn": {"type": "string", "minLength": 1, "maxLength": 600},
    "event_claim": {"type": "string", "minLength": 1, "maxLength": 1000},
    "aggregation_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
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
    "facts": {
        "type": "array",
        "items": object_schema(
            {
                "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
                "supporting_item_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 40,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            ["claim", "supporting_item_ids"],
        ),
    },
    "history_status": {"type": "string", "enum": ["new", "meaningful_update", "repeat", "uncertain"]},
    "prior_event_key": {"type": ["string", "null"]},
    "history_reason": {"type": "string", "minLength": 1, "maxLength": 1000},
    "meaningful_updates": {
        "type": "array",
        "items": object_schema(
            {
                "claim": {"type": "string", "minLength": 1, "maxLength": 1000},
                "supporting_item_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 40,
                    "items": {"type": "integer", "minimum": 1},
                },
            },
            ["claim", "supporting_item_ids"],
        ),
    },
    "publishability": {"type": "string", "enum": ["candidate", "needs_review", "rejected"]},
    "split_reason": {
        "type": ["string", "null"],
        "enum": [
            None,
            "different_model_or_major_version",
            "separate_time_window_actionable",
            "independent_security_policy_or_breaking_change",
            "platform_released_independent_product",
            "standalone_pricing_quota_access_change",
        ],
    },
    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    "caveats": {"type": "array", "items": {"type": "string"}},
}

EVENT_DRAFT_REQUIRED = [
    "draft_key", "event_family_key", "item_ids", "title", "summary_cn", "event_claim",
    "aggregation_reason", "topic", "topics", "keywords", "entities", "facts",
    "history_status", "prior_event_key", "history_reason", "meaningful_updates",
    "publishability", "split_reason", "confidence", "caveats",
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
