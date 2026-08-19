"""Provider parsing and local validation for Stage D v2.

D1 and D3 both require complete provider coverage. Missing rows are errors;
there is intentionally no old provider-response fallback.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable, Mapping, Sequence

from app.ai.skills.intel_triage.parser import unwrap_provider_response

from .models import (
    STAGE_D_REASON_CODE_SET,
    StageDAssessmentResponse,
    StageDCompositionResponse,
    StageDEditorialDecision,
)

_MARKDOWN_OR_URL_RE = re.compile(r"(?:https?://|www\.|\[[^\]]+\]\([^)]*\)|`|<[^>]+>)", re.IGNORECASE)


def strict_parse_stage_d_assessment(
    data: Any,
    *,
    event_ids: Iterable[int],
) -> StageDAssessmentResponse:
    """Parse a D1 response and require exactly one assessment per input ID."""

    result_data, _raw = unwrap_provider_response(data)
    parsed = StageDAssessmentResponse.model_validate(result_data)
    _require_exact_ids(
        actual=[assessment.event_id for assessment in parsed.assessments],
        expected=event_ids,
        label="Stage D assessment",
    )
    return parsed


def strict_parse_stage_d_composition(
    data: Any,
    *,
    event_ids: Iterable[int],
    total_max: int = 30,
    watchlist_max: int = 10,
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]] | None = None,
) -> StageDCompositionResponse:
    """Parse D3 and apply non-negotiable local composition guards."""

    result_data, _raw = unwrap_provider_response(data)
    parsed = StageDCompositionResponse.model_validate(result_data)
    expected = _require_exact_ids(
        actual=[decision.event_id for decision in parsed.decisions],
        expected=event_ids,
        label="Stage D composition",
    )
    decisions = list(parsed.decisions)
    if events is not None:
        candidate_decisions = [decision for decision in decisions if decision.decision in {"selected", "watchlist"}]
        _validate_titles_against_input(candidate_decisions, events)
        by_id = _event_map(events)
        guarded: list[StageDEditorialDecision] = []
        for decision in decisions:
            event = by_id.get(int(decision.event_id))
            if decision.decision in {"selected", "watchlist"} and _recent_history_requires_update(event, decision):
                guarded.append(
                    _local_omitted_decision(
                        decision,
                        reason_code="recent_repeat_without_material_update",
                        reason="本地 guard：近期已展示事件未提供 material_update 理由，暂不重复展示。",
                    )
                )
            else:
                guarded.append(decision)
        decisions = guarded

    selected = sorted(
        (decision for decision in decisions if decision.decision == "selected"),
        key=lambda decision: (int(decision.display_order or 0), int(decision.event_id)),
    )
    watchlist = sorted(
        (decision for decision in decisions if decision.decision == "watchlist"),
        key=lambda decision: (int(decision.display_order or 0), int(decision.event_id)),
    )
    omitted = {decision.event_id: decision for decision in decisions if decision.decision == "omitted"}
    limit = max(0, int(total_max))
    watch_limit = max(0, int(watchlist_max))
    selected_kept: list[StageDEditorialDecision] = []
    selected_guarded: list[StageDEditorialDecision] = []
    selected_family_counts: Counter[str] = Counter()
    for decision in selected:
        if len(selected_kept) >= limit:
            selected_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="composition_limit",
                    reason=f"本地 guard：日报最多保留 {limit} 条 selected。",
                )
            )
        elif selected_family_counts[decision.story_family_id] >= 2:
            selected_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="composition_limit",
                    reason="本地 guard：同一 story_family_id 最多保留两条 selected。",
                )
            )
        else:
            selected_kept.append(decision)
            selected_family_counts[decision.story_family_id] += 1

    watch_kept: list[StageDEditorialDecision] = []
    watch_guarded: list[StageDEditorialDecision] = []
    watch_family_counts: Counter[str] = Counter()
    for decision in watchlist:
        family = decision.story_family_id
        if len(watch_kept) >= watch_limit:
            watch_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="composition_limit",
                    reason=f"本地 guard：观察池最多保留 {watch_limit} 条 watchlist。",
                )
            )
        elif selected_family_counts[family] >= 2:
            watch_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="composition_limit",
                    reason="本地 guard：该 story_family_id 已有两条 selected，不能进入 watchlist。",
                )
            )
        elif watch_family_counts[family] >= 1:
            watch_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="composition_limit",
                    reason="本地 guard：同一 story_family_id 最多保留一条 watchlist。",
                )
            )
        else:
            watch_kept.append(decision)
            watch_family_counts[family] += 1

    normalized: list[StageDEditorialDecision] = []
    selected_family_positions: Counter[str] = Counter()
    for display_order, decision in enumerate(selected_kept, start=1):
        selected_family_positions[decision.story_family_id] += 1
        normalized.append(
            decision.model_copy(
                update={
                    "display_order": display_order,
                    "family_position": selected_family_positions[decision.story_family_id],
                }
            )
        )
    for display_order, decision in enumerate(watch_kept, start=1):
        normalized.append(decision.model_copy(update={"display_order": display_order, "family_position": None}))

    # Keep complete coverage and preserve a deterministic order: selected,
    # watchlist, then provider/local omissions ordered by input ID.
    normalized.extend(selected_guarded)
    normalized.extend(watch_guarded)
    normalized.extend(omitted[event_id] for event_id in expected if event_id in omitted)
    result = StageDCompositionResponse.model_validate(
        {"schema_version": "stage_d_editorial_v2", "decisions": [row.model_dump(mode="json") for row in normalized]}
    )
    return result


def _require_exact_ids(*, actual: Iterable[int], expected: Iterable[int], label: str) -> list[int]:
    expected_list = [int(event_id) for event_id in expected]
    if len(expected_list) != len(set(expected_list)):
        raise ValueError(f"{label} input event_ids contain duplicate IDs")
    actual_list = [int(event_id) for event_id in actual]
    actual_set = set(actual_list)
    expected_set = set(expected_list)
    unknown = sorted(actual_set - expected_set)
    missing = sorted(expected_set - actual_set)
    if unknown:
        raise ValueError(f"{label} decisions contain unknown input ids: {unknown}")
    if missing:
        raise ValueError(f"{label} response is missing input ids: {missing}")
    if len(actual_list) != len(expected_list):
        raise ValueError(f"{label} response contains duplicate event_id")
    return expected_list


def _local_omitted_decision(
    decision: StageDEditorialDecision,
    *,
    reason_code: str,
    reason: str,
) -> StageDEditorialDecision:
    codes = [code for code in decision.reason_codes if code != reason_code][:11]
    codes.append(reason_code)
    return decision.model_copy(
        update={
            "decision": "omitted",
            "display_order": None,
            "family_position": None,
            "display_title_zh": None,
            "title_supporting_fields": [],
            "reason_codes": codes,
            "editorial_reason": reason,
        }
    )


_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?%?(?![\w.])")
_UNSUPPORTED_CERTAINTY_TERMS = (
    "已发布",
    "已确认",
    "已证实",
    "确认",
    "证实",
    "官方已发布",
    "正式发布",
    "已经发布",
    "确认发布",
)
_COMMUNITY_UNCERTAINTY_TERMS = ("社区", "传闻", "据称", "报道称", "消息称", "待核实", "爆料")


def _validate_titles_against_input(
    decisions: Sequence[Any],
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]],
) -> None:
    by_id = _event_map(events)
    for decision in decisions:
        event = by_id.get(int(decision.event_id))
        if event is None:
            raise ValueError(f"Stage D missing title-validation context for event_id={decision.event_id}")
        fields = tuple(decision.title_supporting_fields)
        support = " ".join(str(event.get(field) or "") for field in fields)
        title = str(decision.display_title_zh or "")
        for number in _NUMBER_RE.findall(title):
            if number not in support:
                raise ValueError(f"display_title_zh contains input-unsupported number {number!r}")
        for term in _UNSUPPORTED_CERTAINTY_TERMS:
            if term in title and term not in support:
                raise ValueError(f"display_title_zh contains input-unsupported certainty term {term!r}")
        evidence_level = str(event.get("source_evidence_level") or "")
        if evidence_level in {"single_community_signal", "multi_community_signal"} and not any(
            term in title for term in _COMMUNITY_UNCERTAINTY_TERMS
        ):
            raise ValueError("community-signal selected title must retain an uncertainty cue")


def _recent_history_requires_update(event: Mapping[str, Any] | None, decision: Any) -> bool:
    if event is None:
        return False
    history = event.get("recent_daily_history")
    return bool(
        isinstance(history, Mapping)
        and history.get("appeared_recently")
        and "material_update" not in decision.reason_codes
    )


def _event_map(
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    rows = events.values() if isinstance(events, Mapping) else events
    result: dict[int, Mapping[str, Any]] = {}
    for event in rows:
        try:
            event_id = int(event.get("event_id"))
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        result[event_id] = event
    return result


__all__ = [
    "strict_parse_stage_d_assessment",
    "strict_parse_stage_d_composition",
]
