"""Provider response parsing plus non-negotiable local Stage D validation."""

from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Any, Iterable, Mapping, Sequence

from app.ai.skills.intel_triage.parser import unwrap_provider_response

from .models import StageDEditorialResponse


def strict_parse_stage_d(
    data: Any,
    *,
    event_ids: Iterable[int],
    total_max: int = 30,
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]] | None = None,
) -> StageDEditorialResponse:
    result_data, _raw = unwrap_provider_response(data)
    parsed = StageDEditorialResponse.model_validate(result_data)
    expected = {int(event_id) for event_id in event_ids}
    actual = {decision.event_id for decision in parsed.decisions}
    if actual != expected or len(parsed.decisions) != len(expected):
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(f"Stage D decisions must cover exactly the input ids; missing={missing}, unknown={unknown}")
    selected = [decision for decision in parsed.decisions if decision.decision == "selected"]
    if len(selected) > max(0, int(total_max)):
        raise ValueError("Stage D selected count exceeds total_max")
    expected_orders = list(range(1, len(selected) + 1))
    actual_orders = sorted(decision.display_order for decision in selected if decision.display_order is not None)
    if actual_orders != expected_orders:
        raise ValueError("Stage D selected display_order must be continuous from 1")
    family_counts = Counter(decision.story_family_id for decision in selected)
    overflowing = sorted(key for key, count in family_counts.items() if count > 2)
    if overflowing:
        raise ValueError("Stage D selected more than two events from a story family: " + ", ".join(overflowing))
    family_positions: dict[str, list[int]] = defaultdict(list)
    for decision in selected:
        if decision.family_position is not None:
            family_positions[decision.story_family_id].append(decision.family_position)
    for family, positions in family_positions.items():
        if sorted(positions) != list(range(1, len(positions) + 1)):
            raise ValueError(f"Stage D selected family_position must be continuous for {family}")
    if events is not None:
        _validate_titles_against_input(selected, events)
    return parsed


parse_stage_d_response = strict_parse_stage_d


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
            # Exact ID coverage was already checked above; this branch makes
            # a caller-provided context mistake explicit instead of silently
            # weakening title validation.
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
        history = event.get("recent_daily_history")
        if isinstance(history, Mapping) and history.get("appeared_recently") and "material_update" not in decision.reason_codes:
            raise ValueError("recently displayed selected event requires material_update reason_code")


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


__all__ = ["parse_stage_d_response", "strict_parse_stage_d"]
