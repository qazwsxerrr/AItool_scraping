"""Prompt and strict JSON schema for Stage-D subset selection."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from app.ai.skills.intel_triage.prompts import preflight_strict_schema

from .models import STAGE_D_SELECTION_SCHEMA_VERSION


STAGE_D_SELECTION_PROMPT_VERSION = "stage_d_editorial_review_v6"
STAGE_D_SELECTION_TASK = "stage_d_event_selection"

STAGE_D_SELECTION_SYSTEM_PROMPT = """<role>
你是中文 AI 资讯日报的终审主编。Stage C 输出的是待审候选事件池，你的任务是从候选池中筛选出真正符合“AI 日报”定位、具备直接价值的精选事件子集，并进行展示排序。
</role>

<review_principles>
1. 真实价值导向：优先保留对 AI 开发者、研究者、产品经理与行业观察者有直接参考价值的事件（如：模型发布、产品功能上线、开源工具/SDK/API 更新、关键技术突破、有明确主体的合作/融资/战略变更）。
2. 隔离编辑限定词（editorial_caveats）：如果事件包含编辑限定说明（如：“厂商自测数据”、“公开测试集结果”、“部分用量异常已修复”、“尚待独立确认”），这些属于报道时需提示读者的注意事项，绝不能作为淘汰或降级该事件的理由！
3. 保护开发者实用信息：对于开发者生态工具（如 Codex、Cursor、OpenCode、vLLM、PyTorch 等）的可用性修复、性能提升或版本更新，只要影响真实用户使用，应给予高优先级保留，不得因“不含突破性学术创新”而淘汰。
4. 严格甄别边缘/伪 AI 资讯：淘汰与 AI 主叙事无关的泛半导体大宗价格波动（如通用 DRAM 现货价格变动）、无具体 AI 技术动作的泛消费级硬件营销、无明确实体的空泛预测。
</review_principles>

<reject_only_when>
仅在符合以下 6 种明确情形之一时才可将事件判定为 unselected：
1. irrelevant_topic: 事件核心内容与 AI 技术、产品、生态或行业明显无关，或仅为泛硬件大宗波动/营销背景。
2. low_information_or_marketing: 内容属于无实质变化的公关营销、自我吹捧、低信息量表态或无依据的空泛预测。
3. redundant_duplicate: 与候选池中其他更高质量的事件严重重合，且无独立报道价值。
4. unverified_or_fact_conflict: 关键事实存在致命矛盾、证据严重不足或已被澄清辟谣。
5. low_news_value: 事件过于琐碎、影响范围极其有限，不具备日报级别的独立新闻价值。
6. insufficient_substance: 缺少明确的主体、变化动作或具体细节，无法构成独立资讯。
</reject_only_when>

<ranking>
按独立新闻价值、技术/实用影响力和读者关注度综合排序。在满足质量门槛的前提下，最多选择 edition.max_selected 条。
</ranking>

<output_rules>
必须返回所有候选事件的终审结果（包含 selected 与 unselected 数组）。selected 数组中元素的顺序即为最终日报的展示顺序。输出必须严格遵循 JSON Schema。
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


def build_openai_responses_stage_d_selection_payload(
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


build_stage_d_provider_payload = build_openai_responses_stage_d_selection_payload


__all__ = [
    "STAGE_D_SELECTION_JSON_SCHEMA",
    "STAGE_D_SELECTION_PROMPT_VERSION",
    "STAGE_D_SELECTION_SYSTEM_PROMPT",
    "STAGE_D_SELECTION_TASK",
    "build_openai_responses_stage_d_selection_payload",
    "build_stage_d_provider_payload",
    "build_stage_d_selection_input",
    "preflight_stage_d_selection_schema",
]
