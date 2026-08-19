"""Provider response parsing plus non-negotiable local Stage D validation."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable, Mapping, Sequence

from app.ai.skills.intel_triage.parser import unwrap_provider_response

from .models import StageDDecision, StageDEditorialResponse


def strict_parse_stage_d(
    data: Any,
    *,
    event_ids: Iterable[int],
    total_max: int = 30,
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]] | None = None,
) -> StageDEditorialResponse:
    result_data, _raw = unwrap_provider_response(data)
    result_data = _dedupe_provider_decisions(result_data)
    parsed = StageDEditorialResponse.model_validate(result_data)
    expected = {int(event_id) for event_id in event_ids}
    actual = {decision.event_id for decision in parsed.decisions}
    unknown = sorted(actual - expected)
    if unknown:
        raise ValueError(f"Stage D decisions contain unknown input ids: {unknown}")
    # The provider contract is selected-only to keep the response bounded.  A
    # complete response remains the public local contract, so materialize
    # deterministic omitted rows for every input event absent from the reply.
    missing = sorted(expected - actual)
    if missing:
        parsed = StageDEditorialResponse.model_validate(
            {
                "schema_version": parsed.schema_version,
                "decisions": [
                    *[decision.model_dump(mode="json") for decision in parsed.decisions],
                    *[_provider_omitted_decision(event_id) for event_id in missing],
                ],
            }
        )
    selected = [decision for decision in parsed.decisions if decision.decision == "selected"]
    recent_guarded: list[dict[str, Any]] = []
    # Title/evidence guards remain hard failures even when a row would later
    # be removed by a local cardinality guard.  This prevents malformed
    # provider output from being hidden behind deterministic omission.
    if events is not None:
        _validate_titles_against_input(selected, events)
        by_id = _event_map(events)
        valid_selected: list[StageDDecision] = []
        for decision in selected:
            event = by_id.get(int(decision.event_id))
            if _recent_history_requires_update(event, decision):
                recent_guarded.append(
                    _local_omitted_decision(
                        decision,
                        reason_code="recent_repeat_without_material_update",
                        reason="本地 guard：近期已展示事件未提供 material_update 理由，暂不重复展示。",
                    )
                )
                continue
            valid_selected.append(decision)
        selected = valid_selected

    # Providers occasionally return duplicate/non-contiguous order or family
    # positions.  Use the declared order plus event ID as a deterministic tie
    # breaker, then materialize local omissions for the bounded edition.
    ordered_selected = sorted(
        selected,
        key=lambda decision: (int(decision.display_order or 0), int(decision.event_id)),
    )
    limit = max(0, int(total_max))
    kept: list[StageDDecision] = []
    guarded: list[dict[str, Any]] = []
    family_counts: Counter[str] = Counter()
    for decision in ordered_selected:
        if len(kept) >= limit:
            guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="local_total_limit",
                    reason=f"本地 guard：日报最多保留 {limit} 条 selected。",
                )
            )
            continue
        if family_counts[decision.story_family_id] >= 2:
            guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="local_story_family_limit",
                    reason="本地 guard：同一 story_family_id 最多保留两条。",
                )
            )
            continue
        kept.append(decision)
        family_counts[decision.story_family_id] += 1

    normalized: list[dict[str, Any]] = []
    family_positions: Counter[str] = Counter()
    for display_order, decision in enumerate(kept, start=1):
        family_positions[decision.story_family_id] += 1
        value = decision.model_dump(mode="json")
        value["display_order"] = display_order
        value["family_position"] = family_positions[decision.story_family_id]
        normalized.append(value)
    normalized.extend(recent_guarded)
    normalized.extend(guarded)
    normalized.extend(decision.model_dump(mode="json") for decision in parsed.decisions if decision.decision != "selected")
    return StageDEditorialResponse.model_validate(
        {
            "schema_version": parsed.schema_version,
            "decisions": normalized,
        }
    )


def _provider_omitted_decision(event_id: int) -> dict[str, Any]:
    """Materialize the local full-response row for a selected-only provider reply."""

    return {
        "event_id": int(event_id),
        "decision": "omitted",
        "display_order": None,
        "editorial_score": 0,
        "story_family_id": f"omitted_{int(event_id)}",
        "family_position": None,
        "display_title_zh": None,
        "title_supporting_fields": [],
        "reason_codes": ["provider_omitted"],
        "editorial_reason": "未进入本次编辑展示列表。",
        "confidence": 0,
    }


def _local_omitted_decision(
    decision: Any,
    *,
    reason_code: str,
    reason: str,
) -> dict[str, Any]:
    value = decision.model_dump(mode="json")
    reason_codes = [code for code in decision.reason_codes if code != reason_code][:11]
    reason_codes.append(reason_code)
    value.update(
        {
            "decision": "omitted",
            "display_order": None,
            "family_position": None,
            "display_title_zh": None,
            "title_supporting_fields": [],
            "reason_codes": reason_codes,
            "editorial_reason": reason,
        }
    )
    return value


parse_stage_d_response = strict_parse_stage_d


def _dedupe_provider_decisions(data: Mapping[str, Any]) -> dict[str, Any]:
    """Keep one deterministic provider row per event instead of failing the batch."""

    result = dict(data)
    rows = result.get("decisions")
    if not isinstance(rows, list):
        return result
    winners: dict[int, tuple[int, Mapping[str, Any]]] = {}
    duplicate_ids: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        raw_event_id = row.get("event_id")
        if isinstance(raw_event_id, bool):
            continue
        try:
            event_id = int(raw_event_id)
        except (TypeError, ValueError, OverflowError):
            continue
        current = winners.get(event_id)
        if current is None:
            winners[event_id] = (index, row)
            continue
        duplicate_ids.add(event_id)
        _, previous = current
        previous_key = (
            1 if previous.get("decision") == "selected" else 0,
            int(previous.get("editorial_score") or 0),
            -int(previous.get("display_order") or 10**9),
            -current[0],
        )
        candidate_key = (
            1 if row.get("decision") == "selected" else 0,
            int(row.get("editorial_score") or 0),
            -int(row.get("display_order") or 10**9),
            -index,
        )
        if candidate_key > previous_key:
            winners[event_id] = (index, row)

    if not duplicate_ids:
        return result
    normalized: list[dict[str, Any]] = []
    for event_id, (index, row) in sorted(winners.items(), key=lambda item: item[1][0]):
        value = dict(row)
        if event_id in duplicate_ids:
            codes = value.get("reason_codes")
            if isinstance(codes, list) and "provider_duplicate_event_id" not in codes:
                value["reason_codes"] = [*codes, "provider_duplicate_event_id"]
        normalized.append(value)
    result["decisions"] = normalized
    return result


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


def _recent_history_requires_update(event: Mapping[str, Any] | None, decision: StageDDecision) -> bool:
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


__all__ = ["parse_stage_d_response", "strict_parse_stage_d"]
