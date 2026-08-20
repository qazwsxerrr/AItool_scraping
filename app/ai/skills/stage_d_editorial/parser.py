"""Provider parsing and local validation for the single Stage-D call."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable, Mapping, Sequence

from app.ai.skills.intel_triage.parser import unwrap_provider_response

from .models import StageDEditorialDecision, StageDEditorialResponse


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


def strict_parse_stage_d_editorial(
    data: Any,
    *,
    event_ids: Iterable[int],
    total_max: int = 30,
    watchlist_max: int = 10,
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]] | None = None,
) -> StageDEditorialResponse:
    """Parse one complete Stage-D response and apply local editorial guards."""

    result_data, _raw = unwrap_provider_response(data)
    parsed = StageDEditorialResponse.model_validate(result_data)
    expected = _require_exact_ids(
        actual=[decision.event_id for decision in parsed.decisions],
        expected=event_ids,
        label="Stage D editorial",
    )
    decisions = list(parsed.decisions)
    if events is not None:
        by_id = _event_map(events)
        pre_guarded: list[StageDEditorialDecision] = []
        for decision in decisions:
            event = by_id.get(int(decision.event_id))
            if decision.decision == "selected" and _event_is_community_only(event):
                pre_guarded.append(_local_watchlist_decision(decision))
            elif decision.decision == "selected" and _event_history_unknown(event):
                pre_guarded.append(_local_watchlist_decision(decision).model_copy(update={"editorial_reason": "本地 guard：历史关系未能确定，先进入观察池。"}))
            else:
                pre_guarded.append(decision)
        _validate_titles_against_input(
            [decision for decision in pre_guarded if decision.decision in {"selected", "watchlist"}],
            events,
        )
        guarded: list[StageDEditorialDecision] = []
        for decision in pre_guarded:
            event = by_id.get(int(decision.event_id))
            if decision.decision in {"selected", "watchlist"} and _event_is_low_signal(event):
                guarded.append(
                    _local_omitted_decision(
                        decision,
                        reason_code="low_signal",
                        reason="本地 guard：事件分数低于 60，不进入日报或观察池。",
                    )
                )
            elif decision.decision in {"selected", "watchlist"} and _event_is_repeat_without_update(event):
                guarded.append(
                    _local_omitted_decision(
                        decision,
                        reason_code="recent_repeat_without_material_update",
                        reason="本地 guard：近期已展示事件没有可核实的材料更新。",
                    )
                )
            elif decision.decision in {"selected", "watchlist"} and _recent_history_requires_update(event, decision):
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
        key=lambda decision: (-int(decision.editorial_score), int(decision.event_id)),
    )
    omitted = {decision.event_id: decision for decision in decisions if decision.decision == "omitted"}
    limit = max(0, int(total_max))
    watch_limit = max(0, int(watchlist_max))

    selected_kept: list[StageDEditorialDecision] = []
    selected_guarded: list[StageDEditorialDecision] = []
    selected_family_counts: Counter[str] = Counter()
    for decision in selected:
        family = decision.story_family_id
        if len(selected_kept) >= limit:
            selected_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="editorial_limit",
                    reason=f"本地 guard：日报最多保留 {limit} 条 selected。",
                )
            )
        elif selected_family_counts[family] >= 2:
            selected_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="editorial_limit",
                    reason="本地 guard：同一 story_family_id 最多保留两条 selected。",
                )
            )
        else:
            selected_kept.append(decision)
            selected_family_counts[family] += 1

    watch_kept: list[StageDEditorialDecision] = []
    watch_guarded: list[StageDEditorialDecision] = []
    watch_family_counts: Counter[str] = Counter()
    for decision in watchlist:
        family = decision.story_family_id
        if len(watch_kept) >= watch_limit:
            watch_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="editorial_limit",
                    reason=f"本地 guard：观察池最多保留 {watch_limit} 条 watchlist。",
                )
            )
        elif selected_family_counts[family] >= 2:
            watch_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="editorial_limit",
                    reason="本地 guard：该 story_family_id 已有两条 selected，不能进入 watchlist。",
                )
            )
        elif watch_family_counts[family] >= 1:
            watch_guarded.append(
                _local_omitted_decision(
                    decision,
                    reason_code="editorial_limit",
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
    # Provider watchlist rows deliberately have no display_order. Export assigns
    # a separate watchlist order after selected cards are persisted.
    normalized.extend(watch_kept)
    normalized.extend(selected_guarded)
    normalized.extend(watch_guarded)
    normalized.extend(omitted[event_id] for event_id in expected if event_id in omitted)
    return StageDEditorialResponse.model_validate(
        {"schema_version": "stage_d_editorial_v3", "decisions": [row.model_dump(mode="json") for row in normalized]}
    )


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


def _validate_titles_against_input(
    decisions: Sequence[StageDEditorialDecision],
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]],
) -> None:
    by_id = _event_map(events)
    for decision in decisions:
        if not decision.display_title_zh:
            continue
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
    if str(event.get("novelty_status") or "").casefold() in {"updated", "update", "new"} or event.get("changed_facts") or event.get("delta_summary"):
        return False
    return bool(
        isinstance(history, Mapping)
        and history.get("appeared_recently")
        and "material_update" not in decision.reason_codes
    )


def _event_is_low_signal(event: Mapping[str, Any] | None) -> bool:
    if event is None:
        return False
    if "display_score" not in event or event.get("display_score") is None:
        return False
    try:
        return float(event.get("display_score") or 0) < 60
    except (TypeError, ValueError, OverflowError):
        return False


def _event_is_repeat_without_update(event: Mapping[str, Any] | None) -> bool:
    if event is None:
        return False
    if str(event.get("novelty_status") or "").casefold() == "repeat":
        return True
    history = event.get("recent_daily_history")
    return bool(isinstance(history, Mapping) and history.get("appeared_recently") and not event.get("changed_facts") and not event.get("delta_summary"))


def _event_is_community_only(event: Mapping[str, Any] | None) -> bool:
    return str((event or {}).get("source_evidence_level") or "") in {"single_community_signal", "multi_community_signal"}


def _event_history_unknown(event: Mapping[str, Any] | None) -> bool:
    if event is None or str(event.get("novelty_status") or "").casefold() != "unknown":
        return False
    flags = event.get("risk_flags") or []
    return any(str(flag).startswith("history:") for flag in flags)


def _local_watchlist_decision(decision: StageDEditorialDecision) -> StageDEditorialDecision:
    codes = [code for code in decision.reason_codes if code != "community_signal"][:11]
    codes.append("community_signal")
    return decision.model_copy(
        update={
            "decision": "watchlist",
            "display_order": None,
            "family_position": None,
            "display_title_zh": None,
            "title_supporting_fields": [],
            "reason_codes": codes,
            "editorial_reason": "本地 guard：非一手社区事件只进入观察池。",
        }
    )


def _event_map(
    events: Sequence[Mapping[str, Any]] | Mapping[int, Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    if isinstance(events, Mapping):
        return {int(key): value for key, value in events.items()}
    return {int(event["event_id"]): event for event in events}


__all__ = ["strict_parse_stage_d_editorial"]
