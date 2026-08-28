"""Business-model parsers for independent Stage A and Stage B calls."""

from __future__ import annotations

from typing import Any, Mapping

from .guards import apply_analysis_guards, apply_screen_guard
from .models import AnalysisResult, RawIntelEnvelope, ScreenResult, normalize_content_class


def strict_parse_screen(
    data: Any,
    *,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
    reject_threshold: int = 85,
    raw_response: Mapping[str, Any] | None = None,
) -> ScreenResult:
    result_data = _business_mapping(data, label="screen")
    original_data = dict(result_data)
    item = _as_envelope(envelope) if envelope is not None else None
    if item is not None and "item_id" not in result_data and item.item_id is not None:
        result_data["item_id"] = item.item_id
    if source_content_class is not None and normalize_content_class(source_content_class) is None:
        raise ValueError("source_content_class is not supported")
    result_data["raw_response"] = dict(raw_response) if raw_response is not None else original_data
    parsed = ScreenResult.model_validate(result_data)
    return apply_screen_guard(parsed, item, reject_threshold=reject_threshold)


def parse_screen_result(
    data: Any,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
    *,
    reject_threshold: int = 85,
) -> ScreenResult:
    return strict_parse_screen(
        data,
        envelope=envelope,
        source_content_class=source_content_class,
        reject_threshold=reject_threshold,
    )


def strict_parse_analysis(
    data: Any,
    *,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    raw_response: Mapping[str, Any] | None = None,
) -> AnalysisResult:
    result_data = _business_mapping(data, label="analysis")
    original_data = dict(result_data)
    item = _as_envelope(envelope) if envelope is not None else None
    if item is not None and "item_id" not in result_data and item.item_id is not None:
        result_data["item_id"] = item.item_id
    result_data["raw_response"] = dict(raw_response) if raw_response is not None else original_data
    parsed = AnalysisResult.model_validate(result_data)
    return apply_analysis_guards(parsed, item)


def parse_analysis_result(
    data: Any,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
) -> AnalysisResult:
    return strict_parse_analysis(data, envelope=envelope)


def _as_envelope(value: RawIntelEnvelope | Mapping[str, Any]) -> RawIntelEnvelope:
    return value if isinstance(value, RawIntelEnvelope) else RawIntelEnvelope.model_validate(value)


def _business_mapping(data: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"Intel {label} result must be a JSON object")
    return dict(data)


__all__ = [
    "parse_analysis_result",
    "parse_screen_result",
    "strict_parse_analysis",
    "strict_parse_screen",
]
