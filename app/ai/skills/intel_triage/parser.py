"""Strict provider-response parsing for the Intel Triage contract."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .guards import apply_deterministic_guards
from .models import (
    RawIntelEnvelope,
    TriageResult,
    normalize_content_class,
)


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", flags=re.IGNORECASE | re.DOTALL)
_REQUIRED_FIELDS = (
    "keep",
    "topic",
    "summary_cn",
    "keywords",
    "selection_score",
    "novelty",
    "paper_support",
    "risk_flags",
)


def unwrap_provider_response(data: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(result_mapping, raw_mapping)`` for common provider envelopes."""

    raw_mapping = _coerce_mapping(data)
    result = _unwrap_mapping(raw_mapping)
    return result, raw_mapping


def strict_parse_triage(
    data: Any,
    *,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
) -> TriageResult:
    """Parse one provider response and apply deterministic guards.

    Structural fields are required and unknown fields are rejected by
    :class:`TriageResult`; field values are normalized only where doing so is
    safe (bounded scores, list cleanup and known aliases).
    """

    result_data, raw_mapping = unwrap_provider_response(data)
    # ``_unwrap_mapping`` may return its input mapping for direct responses;
    # work on a copy so adding raw_response cannot create a self-referential
    # audit object.
    result_data = dict(result_data)
    # A provider may use ``topics`` as the plural equivalent of ``topic`` and
    # ``summary``/``key_terms``/``novelty_status`` as aliases; report a missing
    # field only after checking those safe equivalents.
    missing: list[str] = []
    if "keep" not in result_data:
        missing.append("keep")
    if "topic" not in result_data and not result_data.get("topics") and not result_data.get("topic_labels"):
        missing.append("topic")
    if "summary_cn" not in result_data and "summary" not in result_data and "summary_zh" not in result_data:
        missing.append("summary_cn")
    if "keywords" not in result_data and "key_terms" not in result_data and "tags" not in result_data:
        missing.append("keywords")
    if not any(key in result_data for key in ("selection_score", "score", "display_score", "total_score")):
        missing.append("selection_score")
    if "novelty" not in result_data and "novelty_status" not in result_data:
        missing.append("novelty")
    if "paper_support" not in result_data and "paper" not in result_data and "paper_evidence" not in result_data:
        missing.append("paper_support")
    if "risk_flags" not in result_data and "risks" not in result_data and "risk" not in result_data:
        missing.append("risk_flags")
    if missing:
        # Preserve order while avoiding duplicate names in the diagnostic.
        missing_unique = list(dict.fromkeys(missing))
        raise ValueError("Intel triage response is missing required fields: " + ", ".join(missing_unique))

    source_class = source_content_class
    raw_envelope: RawIntelEnvelope | None = None
    if envelope is not None:
        raw_envelope = envelope if isinstance(envelope, RawIntelEnvelope) else RawIntelEnvelope.model_validate(envelope)
        if source_class is None:
            source_class = raw_envelope.source_content_class
        if "item_id" not in result_data and raw_envelope.item_id is not None:
            result_data["item_id"] = raw_envelope.item_id
        if "source_group" not in result_data and raw_envelope.source_group:
            result_data["source_group"] = raw_envelope.source_group

    normalized_source = normalize_content_class(source_class) if source_class is not None else None
    if source_class is not None and normalized_source is None:
        raise ValueError("source_content_class is not supported")
    if normalized_source is not None:
        result_data["content_class"] = normalized_source
    result_data["raw_response"] = dict(raw_mapping)
    parsed = TriageResult.model_validate(result_data)
    if raw_envelope is not None:
        parsed = parsed.with_item(raw_envelope)
    return apply_deterministic_guards(parsed, raw_envelope)


def parse_triage_result(
    data: Any,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
) -> TriageResult:
    """Compatibility alias for :func:`strict_parse_triage`."""

    return strict_parse_triage(
        data,
        envelope=envelope,
        source_content_class=source_content_class,
    )


def parse_triage_response(
    data: Any,
    envelope: RawIntelEnvelope | Mapping[str, Any] | None = None,
    source_content_class: str | None = None,
) -> TriageResult:
    """Descriptive alias used by provider adapters."""

    return strict_parse_triage(
        data,
        envelope=envelope,
        source_content_class=source_content_class,
    )


def _coerce_mapping(data: Any) -> dict[str, Any]:
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, str):
        parsed = _parse_json_text(data)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ValueError("Intel triage API response must be a JSON object")


def _unwrap_mapping(data: dict[str, Any]) -> dict[str, Any]:
    # Direct result first, then generic envelope fields.
    for key in ("result", "data", "response"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, Mapping):
            nested = dict(value)
            if not _looks_like_result(nested):
                return _unwrap_mapping(nested)
            return nested
        if isinstance(value, str):
            parsed = _parse_json_text(value)
            if isinstance(parsed, Mapping):
                return dict(parsed)
        raise ValueError(f"Intel triage {key} must be a JSON object")

    if "output" in data:
        value = data["output"]
        if isinstance(value, Mapping):
            nested = dict(value)
            return _unwrap_mapping(nested) if not _looks_like_result(nested) else nested
        if isinstance(value, str):
            parsed = _parse_json_text(value)
            if isinstance(parsed, Mapping):
                return dict(parsed)
        if isinstance(value, list):
            text = _output_text(value)
            if text is None:
                raise ValueError("Intel triage Responses response has no output text")
            parsed = _parse_json_text(text)
            if isinstance(parsed, Mapping):
                return dict(parsed)
        raise ValueError("Intel triage output must be a JSON object")

    if "choices" in data:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("Intel triage OpenAI response has no choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("Intel triage choices[0] must be an object")
        message = first.get("message")
        if isinstance(message, Mapping):
            if isinstance(message.get("parsed"), Mapping):
                return dict(message["parsed"])
            content = message.get("content")
        else:
            content = first.get("text")
        if isinstance(content, Mapping) and isinstance(content.get("parsed"), Mapping):
            return dict(content["parsed"])
        if isinstance(content, Mapping) and "keep" in content:
            return dict(content)
        text = _content_to_text(content)
        if text is None:
            raise ValueError("Intel triage OpenAI response has no JSON content")
        parsed = _parse_json_text(text)
        if not isinstance(parsed, Mapping):
            raise ValueError("Intel triage OpenAI content must be a JSON object")
        return dict(parsed)

    if "output_text" in data:
        text = _content_to_text(data.get("output_text"))
        if text is None:
            raise ValueError("Intel triage output_text is empty")
        parsed = _parse_json_text(text)
        if not isinstance(parsed, Mapping):
            raise ValueError("Intel triage output_text must be a JSON object")
        return dict(parsed)

    return data


def _looks_like_result(value: Mapping[str, Any]) -> bool:
    return any(key in value for key in ("keep", "topic", "topics", "summary_cn", "summary"))


def _output_text(value: list[Any]) -> str | None:
    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if isinstance(part.get("text"), str):
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


def _parse_json_text(value: str) -> Any:
    text = value.strip()
    match = _JSON_FENCE_RE.match(text)
    if match:
        text = match.group(1).strip()
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Intel triage API returned invalid JSON content") from exc


__all__ = [
    "parse_triage_response",
    "parse_triage_result",
    "strict_parse_triage",
    "unwrap_provider_response",
]
