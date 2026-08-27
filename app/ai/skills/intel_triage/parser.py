"""Strict business parsers for independent Stage A and Stage B calls."""

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
) -> ScreenResult:
    if not isinstance(data, Mapping):
        raise ValueError("Intel screen response must be a JSON object")
    raw_mapping = dict(data)
    result_data = dict(raw_mapping)
    missing = [key for key in ("decision", "reason_code", "reason", "confidence", "risk_flags") if key not in result_data]
    if missing:
        raise ValueError("Intel screen response is missing required fields: " + ", ".join(missing))
    item = _as_envelope(envelope) if envelope is not None else None
    if item is not None and "item_id" not in result_data and item.item_id is not None:
        result_data["item_id"] = item.item_id
    # ``source_content_class`` is intentionally not a model field for Stage A;
    # validate it here when a caller supplies the optional provenance hint.
    if source_content_class is not None and normalize_content_class(source_content_class) is None:
        raise ValueError("source_content_class is not supported")
    result_data["raw_response"] = dict(raw_mapping)
    parsed = ScreenResult.model_validate(result_data)
    return apply_screen_guard(parsed, item, reject_threshold=reject_threshold)


def parse_screen_result(
    data: Any,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
    *,
    reject_threshold: int = 85,
) -> ScreenResult:
    return strict_parse_screen(data, envelope=envelope, source_content_class=source_content_class, reject_threshold=reject_threshold)


def strict_parse_analysis(
    data: Any,
    *,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
) -> AnalysisResult:
    if not isinstance(data, Mapping):
        raise ValueError("Intel analysis response must be a JSON object")
    raw_mapping = dict(data)
    result_data = dict(raw_mapping)
    required_fields = ("topic", "topics", "summary_cn", "keywords", "entities", "b1_priority", "score_components")
    missing = [name for name in required_fields if name not in result_data]
    if missing:
        raise ValueError("Intel analysis response is missing required fields: " + ", ".join(missing))

    components = result_data["score_components"]
    required_components = (
        "audience_relevance",
        "material_change",
        "impact_scope",
        "independent_news_value",
        "specificity",
    )
    if not isinstance(components, Mapping):
        raise ValueError("Intel analysis score_components must be an object")
    missing_components = [name for name in required_components if name not in components]
    if missing_components:
        raise ValueError("Intel analysis score_components is missing required fields: " + ", ".join(missing_components))

    item = _as_envelope(envelope) if envelope is not None else None
    if item is not None:
        if "item_id" not in result_data and item.item_id is not None:
            result_data["item_id"] = item.item_id
    result_data["raw_response"] = dict(raw_mapping)
    parsed = AnalysisResult.model_validate(result_data)
    return apply_analysis_guards(parsed, item)


def parse_analysis_result(
    data: Any,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
) -> AnalysisResult:
    return strict_parse_analysis(data, envelope=envelope)


def normalize_screen_provider_output(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply only unambiguous Stage-A provider compatibility repairs."""

    result = dict(data)
    transformations: list[str] = []
    if "risk_flags" not in result and "risks" in result:
        result["risk_flags"] = result.pop("risks")
        transformations.append("alias:risks->risk_flags")
    confidence = _number(result.get("confidence"))
    if confidence is not None and 0 < confidence < 1:
        result["confidence"] = int(round(confidence * 100))
        transformations.append("scale:confidence:0-1->0-100")
    return result, transformations


def normalize_analysis_provider_output(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize a clearly consistent 0-1 Stage-B score vector."""

    result = dict(data)
    components = result.get("score_components")
    if not isinstance(components, Mapping):
        return result, []
    fields = (
        "audience_relevance",
        "material_change",
        "impact_scope",
        "independent_news_value",
        "specificity",
    )
    values = [_number(components.get(field)) for field in fields]
    if any(value is None or value < 0 or value > 1 for value in values):
        return result, []
    if not any(value not in {0.0, 1.0} for value in values if value is not None):
        raise ValueError("Intel analysis score_components use an ambiguous 0/1 scale; return explicit 0-100 scores")
    normalized_components = dict(components)
    for field, value in zip(fields, values, strict=True):
        normalized_components[field] = int(round(float(value) * 100))
    result["score_components"] = normalized_components
    priority = _number(result.get("b1_priority"))
    if priority is not None and 0 <= priority <= 1:
        result["b1_priority"] = int(round(priority * 100))
    return result, ["scale:score_components:0-1->0-100"]


def _as_envelope(value: RawIntelEnvelope | Mapping[str, Any]) -> RawIntelEnvelope:
    return value if isinstance(value, RawIntelEnvelope) else RawIntelEnvelope.model_validate(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


__all__ = [
    "normalize_analysis_provider_output", "normalize_screen_provider_output",
    "parse_analysis_result", "parse_screen_result",
    "strict_parse_analysis", "strict_parse_screen",
]
