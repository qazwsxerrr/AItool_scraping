"""Strict provider-response parsers for independent Stage A and Stage B calls."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .guards import apply_analysis_guards, apply_screen_guard
from .models import AnalysisResult, RawIntelEnvelope, ScreenResult, normalize_content_class


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", flags=re.IGNORECASE | re.DOTALL)


def unwrap_provider_response(data: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(result_mapping, raw_mapping)`` for common JSON providers."""

    raw_mapping = _coerce_mapping(data, label="intel")
    return _unwrap_mapping(raw_mapping), raw_mapping


def strict_parse_screen(
    data: Any,
    *,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
    reject_threshold: int = 85,
) -> ScreenResult:
    result_data, raw_mapping = unwrap_provider_response(data)
    result_data = dict(result_data)
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
    source_content_class: str | None = None,
) -> AnalysisResult:
    result_data, raw_mapping = unwrap_provider_response(data)
    result_data = dict(result_data)
    missing: list[str] = []
    if "topic" not in result_data and not result_data.get("topics") and not result_data.get("topic_labels"):
        missing.append("topic")
    if "summary_cn" not in result_data and "summary" not in result_data:
        missing.append("summary_cn")
    if "keywords" not in result_data and "key_terms" not in result_data and "tags" not in result_data:
        missing.append("keywords")
    if "entities" not in result_data and "typed_entities" not in result_data:
        missing.append("entities")
    if not any(key in result_data for key in ("selection_score", "score", "display_score", "total_score")):
        missing.append("selection_score")
    if not any(key in result_data for key in ("score_components", "scores", "score_breakdown")):
        missing.append("score_components")
    if "paper_support" not in result_data and "paper" not in result_data and "paper_evidence" not in result_data:
        missing.append("paper_support")
    if "risk_flags" not in result_data and "risks" not in result_data and "risk" not in result_data:
        missing.append("risk_flags")
    if "reason" not in result_data:
        missing.append("reason")
    if "confidence" not in result_data:
        missing.append("confidence")
    if missing:
        raise ValueError("Intel analysis response is missing required fields: " + ", ".join(dict.fromkeys(missing)))

    item = _as_envelope(envelope) if envelope is not None else None
    if item is not None:
        if "item_id" not in result_data and item.item_id is not None:
            result_data["item_id"] = item.item_id
        if "source_content_class" not in result_data:
            result_data["source_content_class"] = item.source_content_class
        if "source_group" not in result_data and item.source_group:
            result_data["source_group"] = item.source_group
    if source_content_class is not None:
        normalized_source = normalize_content_class(source_content_class)
        if normalized_source is None:
            raise ValueError("source_content_class is not supported")
        result_data["source_content_class"] = normalized_source
    # Some providers emit explicit null for the required paper provenance
    # token on non-paper items.  Keep the strict public contract while
    # normalizing that harmless null to the model's neutral value instead of
    # blocking the whole Stage-B run.
    paper_support = result_data.get("paper_support")
    if isinstance(paper_support, Mapping):
        paper_support = dict(paper_support)
        if paper_support.get("source_type") is None:
            paper_support["source_type"] = "unknown"
        result_data["paper_support"] = paper_support
    result_data["raw_response"] = dict(raw_mapping)
    parsed = AnalysisResult.model_validate(result_data)
    return apply_analysis_guards(parsed, item)


def parse_analysis_result(
    data: Any,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
) -> AnalysisResult:
    return strict_parse_analysis(data, envelope=envelope, source_content_class=source_content_class)


# Descriptive aliases used by callers that name the transport operation rather
# than the provider-neutral result class.
parse_screen_response = parse_screen_result
parse_analysis_response = parse_analysis_result


def _as_envelope(value: RawIntelEnvelope | Mapping[str, Any]) -> RawIntelEnvelope:
    return value if isinstance(value, RawIntelEnvelope) else RawIntelEnvelope.model_validate(value)


def _coerce_mapping(data: Any, *, label: str) -> dict[str, Any]:
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, str):
        parsed = _parse_json_text(data, label=label)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ValueError(f"Intel {label} API response must be a JSON object")


def _unwrap_mapping(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("result", "data", "response"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, Mapping):
            nested = dict(value)
            return _unwrap_mapping(nested) if not _looks_like_result(nested) else nested
        if isinstance(value, str):
            parsed = _parse_json_text(value, label="provider")
            if isinstance(parsed, Mapping):
                return dict(parsed)
        raise ValueError(f"Intel provider {key} must be a JSON object")
    if "output" in data:
        value = data["output"]
        if isinstance(value, Mapping):
            nested = dict(value)
            return _unwrap_mapping(nested) if not _looks_like_result(nested) else nested
        if isinstance(value, str):
            parsed = _parse_json_text(value, label="provider")
            if isinstance(parsed, Mapping):
                return dict(parsed)
        if isinstance(value, list):
            text = _output_text(value)
            if text is not None:
                parsed = _parse_json_text(text, label="provider")
                if isinstance(parsed, Mapping):
                    return dict(parsed)
        raise ValueError("Intel Responses response has no output JSON")
    if "choices" in data:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("Intel OpenAI response has no choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("Intel OpenAI choices[0] must be an object")
        message = first.get("message")
        content = message.get("content") if isinstance(message, Mapping) else first.get("text")
        if isinstance(message, Mapping) and isinstance(message.get("parsed"), Mapping):
            return dict(message["parsed"])
        if isinstance(content, Mapping) and isinstance(content.get("parsed"), Mapping):
            return dict(content["parsed"])
        text = _content_to_text(content)
        if text is None:
            raise ValueError("Intel OpenAI response has no JSON content")
        parsed = _parse_json_text(text, label="provider")
        if isinstance(parsed, Mapping):
            return dict(parsed)
        raise ValueError("Intel OpenAI content must be a JSON object")
    if "output_text" in data:
        text = _content_to_text(data.get("output_text"))
        if text is None:
            raise ValueError("Intel output_text is empty")
        parsed = _parse_json_text(text, label="provider")
        if isinstance(parsed, Mapping):
            return dict(parsed)
        raise ValueError("Intel output_text must be a JSON object")
    return data


def _looks_like_result(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in ("decision", "reason_code", "topic", "topics", "summary_cn", "summary"))


def _output_text(value: list[Any]) -> str | None:
    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if isinstance(item.get("text"), str):
            parts.append(item["text"])
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    text = "".join(parts).strip()
    return text or None


def _content_to_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("text")
        return text if isinstance(text, str) else None
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts) or None
    return None


def _parse_json_text(value: str, *, label: str) -> Any:
    text = value.strip()
    match = _JSON_FENCE_RE.match(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Intel {label} API returned invalid JSON content") from exc


__all__ = [
    "parse_analysis_response", "parse_analysis_result", "parse_screen_response", "parse_screen_result",
    "strict_parse_analysis", "strict_parse_screen",
    "unwrap_provider_response",
]
