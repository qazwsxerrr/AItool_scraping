"""Provider parsing and subset validation for Stage-D selection."""

from __future__ import annotations

from typing import Any, Iterable

from app.ai.skills.intel_triage.parser import unwrap_provider_response

from .models import StageDSelectionResponse


def strict_parse_stage_d_selection(
    data: Any,
    *,
    candidate_event_ids: Iterable[int],
    max_selected: int,
) -> StageDSelectionResponse:
    """Parse an ordered subset without re-evaluating Stage-C eligibility."""

    candidate_ids = [int(event_id) for event_id in candidate_event_ids]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("Stage D candidate_event_ids contain duplicate IDs")

    result_data, _raw = unwrap_provider_response(data)
    parsed = StageDSelectionResponse.model_validate(result_data)
    candidate_set = set(candidate_ids)
    returned_ids = [row.event_id for row in parsed.selected] + [row.event_id for row in parsed.unselected]
    unknown = sorted(event_id for event_id in returned_ids if event_id not in candidate_set)
    if unknown:
        raise ValueError(f"Stage D returned unknown candidate event_ids: {unknown}")
    limit = max(0, int(max_selected))
    if len(parsed.selected) > limit:
        raise ValueError(
            f"Stage D selected {len(parsed.selected)} events, exceeding max_selected={limit}"
        )
    missing = [event_id for event_id in candidate_ids if event_id not in set(returned_ids)]
    if missing:
        raise ValueError(f"Stage D did not return decisions for candidate event_ids: {missing}")
    return parsed


__all__ = ["strict_parse_stage_d_selection"]
