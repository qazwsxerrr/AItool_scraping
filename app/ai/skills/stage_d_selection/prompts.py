"""Prompt and strict JSON schema for Stage-D subset selection."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from app.ai.skills.intel_triage.prompts import preflight_strict_schema

from .models import STAGE_D_SELECTION_SCHEMA_VERSION


STAGE_D_SELECTION_PROMPT_VERSION = "stage_d_editorial_review_v14"
STAGE_D_SELECTION_TASK = "stage_d_event_selection"

STAGE_D_SELECTION_SYSTEM_PROMPT = """<role>
你是中文 AI 资讯日报的终审编辑。Stage C 已完成时间准入、事件聚合、事实边界收窄和初步资格判断。你的任务是按读者价值组织最终日报：从候选事件中保留值得报道的有序子集，并形成有重点、有栏目感的阅读顺序。这是 AI 资讯日报，不是调查报道——对无法独立确认但来源可归因的消息使用"据称"、"消息称"等限定词即可报道，不需要新闻级核实标准。
</role>

<review_principles>
1. 先逐条判断是否值得报道，再对保留事件排序。
2. edition.max_selected 是硬上限，不是目标；edition.soft_selected_target 是常规日报建议长度。候选明显超过 soft_selected_target 时，应主动压缩低优先级、弱来源、重复故事族和低信息密度条目；但当天高价值事件很多时可以超过 soft_selected_target，不能超过 max_selected。
3. publishability=candidate 表示 Stage C 已形成可追溯事件包。对此类事件应默认保留，除非存在明确、可说明的淘汰原因。
4. 模型、API 和开发工具的可用性、价格、额度、访问范围、订阅权益、重要修复、版本更新通常对读者最可执行，应比同等强度的产业/研究/安全条目更靠前；但这不是关键词硬规则，仍要看事件完整性、来源强度、影响范围和信息密度。不要只因标题命中某些词就保留，也不要只因没有命中这些词就淘汰。
5. AI 基础设施、芯片、算力、产业链和研发突破是日报正常内容。若有明确发布、量产、部署、性能数据、供应链变化或战略意义，应保留；但同一公司或同一故事族集中爆发时，优先保留最有增量的 1-3 条，避免连续刷屏。
6. 官方来源或可信媒体披露的融资洽谈、路线图、计划、传闻可以进入日报，但应以"据报/计划/预计"等口径报道，排序通常低于已可用的模型、API 和开发工具变化。低质社区转述或无法追溯的传闻应淘汰。
7. editorial_caveats 只用于限定报道口径（如"厂商自测数据"、"公开测试集结果"、"部分异常已修复"），绝不能作为淘汰或降级事件的理由；但 caveat 可影响排序和是否放入主稿。
8. eligibility_blockers 非空时可以淘汰。
</review_principles>

<ranking>
排序目标是像日报编辑而不是分数榜：
1. 今日要闻优先：重大新模型/API 上线、开发工具实用变化、额度/价格/访问范围变化、关键可用性修复。
2. 然后是直接可用的产品能力、插件/平台接入和重要生态变化。
3. 再是 AI 基建、芯片、算力、产业链和研发突破；重大事件可进入前列，但同一故事族不要连续占满头部。
4. 之后是行业合作、融资、治理、安全和研究趋势。
5. 社区转述、单方 benchmark、薄演示、轻量文档更新、低信息榜单通常靠后或不选。
综合使用标题、摘要、facts、event_family_key、topic、source_groups、history_status、publishability、eligibility_blockers、editorial_caveats 和内容互补性。不要把分数、过程态核验或关键词命中当成独立淘汰规则。排序不改变事件是否合格的独立判断。
</ranking>

<output_rules>
必须返回所有候选事件的终审结果：每个 candidate_events 中的 event_id 必须且只能出现在 selected 或 unselected 之一。selected 数组中元素的顺序即为最终日报的展示顺序。输出必须严格遵循 JSON Schema。
</output_rules>"""


STAGE_D_SELECTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "selected", "unselected"],
    "properties": {
        "schema_version": {
            "type": "string",
            "enum": [STAGE_D_SELECTION_SCHEMA_VERSION],
        },
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event_id", "reason_code", "reason"],
                "properties": {
                    "event_id": {"type": "integer", "minimum": 1},
                    "reason_code": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[a-z][a-z0-9_]*$",
                    },
                    "reason": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
        },
        "unselected": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event_id", "reason_code", "reason"],
                "properties": {
                    "event_id": {"type": "integer", "minimum": 1},
                    "reason_code": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[a-z][a-z0-9_]*$",
                    },
                    "reason": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
        },
    },
}


def build_stage_d_provider_payload(
    events: Sequence[Mapping[str, Any]],
    *,
    edition: Mapping[str, Any] | None = None,
    max_selected: int = 15,
    model: str | None = None,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    instructions = STAGE_D_SELECTION_SYSTEM_PROMPT
    if profile:
        profile_text = json.dumps(profile, ensure_ascii=False, indent=2)
        instructions += f"\n\n<editorial_profile>\n{profile_text}\n</editorial_profile>"

    user_payload = {
        "edition": edition or {"edition_date": "2026-08-24", "max_selected": max_selected},
        "max_selected": max_selected,
        "candidate_events": events,
    }
    return {
        "model": model,
        "input": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": STAGE_D_SELECTION_TASK,
                "strict": True,
                "schema": STAGE_D_SELECTION_JSON_SCHEMA,
            }
        },
    }


def preflight_stage_d_selection_schema() -> bool:
    return preflight_strict_schema(STAGE_D_SELECTION_JSON_SCHEMA, path="stage_d_selection")


def build_stage_d_selection_input(
    events: Sequence[Mapping[str, Any]],
    *,
    max_selected: int = 15,
    profile: Mapping[str, Any] | None = None,
) -> str:
    user_payload = {
        "max_selected": max_selected,
        "candidate_events": events,
    }
    return json.dumps(user_payload, ensure_ascii=False, default=str)


__all__ = [
    "STAGE_D_SELECTION_JSON_SCHEMA",
    "STAGE_D_SELECTION_PROMPT_VERSION",
    "STAGE_D_SELECTION_SYSTEM_PROMPT",
    "STAGE_D_SELECTION_TASK",
    "build_stage_d_provider_payload",
    "build_stage_d_selection_input",
    "preflight_stage_d_selection_schema",
]
