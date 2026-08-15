"""Schemas and deterministic parsing rules for one-pass item analysis.

This module contains no network or provider-specific code.  It is the boundary
between untrusted model JSON and the rest of the ingestion pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


ContentClass: TypeAlias = Literal[
    "official_model_company",
    "project_tool",
    "community_social",
]

OFFICIAL_MODEL_COMPANY = "official_model_company"
PROJECT_TOOL = "project_tool"
COMMUNITY_SOCIAL = "community_social"

CONTENT_CLASSES: tuple[str, ...] = (
    OFFICIAL_MODEL_COMPANY,
    PROJECT_TOOL,
    COMMUNITY_SOCIAL,
)

ITEM_ANALYSIS_RESPONSE_SCHEMA: dict[str, str] = {
    "keep": "boolean",
    "content_class": "official_model_company|project_tool|community_social",
    "topic_category": "string; one of the configured topic categories",
    "summary_cn": "string",
    "reason": "string",
    "risk_flags": "array<string>",
    "confidence": "integer 0-100",
}

PROJECT_SUMMARY_RESPONSE_SCHEMA: dict[str, str] = {
    "summary_cn": "string",
    "capabilities": "array<string>",
    "use_cases": "array<string>",
    "risk_flags": "array<string>",
}

class ItemAnalysisRequest(BaseModel):
    """Normalized item data supplied to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int | str
    title: str
    url: str | None
    source_id: str
    source_content_class: ContentClass
    body_preview: str
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _validate_input(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        item_id = data.get("item_id")
        if item_id is None or (isinstance(item_id, str) and not item_id.strip()):
            raise ValueError("item_id is required")
        if not isinstance(data.get("metrics", {}), dict):
            raise TypeError("metrics must be a dict")
        source_class = normalize_content_class(data.get("source_content_class"))
        if source_class is None:
            raise ValueError(
                "source_content_class must be one of: " + ", ".join(CONTENT_CLASSES)
            )
        data["source_content_class"] = source_class
        data["metrics"] = dict(data.get("metrics") or {})
        return data


class ItemAnalysisResponse(BaseModel):
    """Validated provider-neutral result.

    ``raw_response`` is deliberately retained for audit/debugging.  It is never
    treated as an external fact by this module.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    keep: bool
    content_class: ContentClass
    topic_category: str = "未分类"
    summary_cn: str
    reason: str
    risk_flags: list[str] = Field(default_factory=list)
    confidence: int = 0
    raw_response: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        content_class = normalize_content_class(data.get("content_class"))
        if content_class is None:
            raise ValueError("content_class is not supported")
        raw_response = data.get("raw_response")
        if raw_response is not None and not isinstance(raw_response, dict):
            raise TypeError("raw_response must be a dict or None")
        data.update(
            content_class=content_class,
            topic_category=clean_text(data.get("topic_category") or data.get("category")) or "未分类",
            keep=coerce_bool(data.get("keep"), default=False),
            summary_cn=clean_text(data.get("summary_cn")),
            reason=clean_text(data.get("reason")),
            risk_flags=clean_string_list(data.get("risk_flags")),
            confidence=clamp_int(data.get("confidence")),
        )
        return data


def apply_local_guard(
    response: ItemAnalysisResponse,
    source_content_class: str | None = None,
) -> ItemAnalysisResponse:
    """Apply deterministic safety rules after model parsing.

    When a source category is provided, it is authoritative: the model cannot
    switch an item into a different source class. The guard never upgrades
    ``keep`` or confidence and never removes raw provider data.
    """

    final_class = response.content_class
    if source_content_class is not None:
        source_class = normalize_content_class(source_content_class)
        if source_class is None:
            raise ValueError("source_content_class must be one of: " + ", ".join(CONTENT_CLASSES))
        final_class = source_class

    if final_class != response.content_class:
        return response.model_copy(
            update={
                "content_class": final_class,
            }
        )
    return response


# More explicit alias for callers that use the phrase from the pipeline docs.
guard_item_analysis_response = apply_local_guard


def parse_item_analysis_response(
    data: Any,
    source_content_class: str,
    allowed_categories: list[str] | tuple[str, ...] | None = None,
) -> ItemAnalysisResponse:
    """Unwrap, strictly parse, normalize, and guard a provider response.

    Envelope/JSON failures raise ``ValueError``.  Individual field quirks are
    normalized to safe defaults. The source category is always authoritative;
    the model category remains only in ``raw_response`` for auditing.
    """

    raw = _coerce_raw_mapping(data)
    result = _unwrap_response(raw)
    # ``topic_category`` was added after the original AI-only contract. Keep
    # it optional at the parser boundary so old provider responses and audit
    # rows remain readable; the job assigns a deterministic fallback.
    required_fields = [key for key in ITEM_ANALYSIS_RESPONSE_SCHEMA if key != "topic_category"]
    missing = [key for key in required_fields if key not in result]
    if missing:
        raise ValueError("Item analysis response is missing required fields: " + ", ".join(missing))
    fallback_class = normalize_content_class(source_content_class)
    if fallback_class is None:
        raise ValueError("source_content_class must be one of: " + ", ".join(CONTENT_CLASSES))

    response = ItemAnalysisResponse(
        keep=coerce_bool(result.get("keep"), default=False),
        content_class=fallback_class,
        topic_category=normalize_topic_category(result.get("topic_category") or result.get("category"), allowed_categories),
        summary_cn=clean_text(result.get("summary_cn")),
        reason=clean_text(result.get("reason")),
        risk_flags=clean_string_list(result.get("risk_flags")),
        confidence=clamp_int(result.get("confidence")),
        raw_response=raw,
    )
    return apply_local_guard(response, fallback_class)


def normalize_topic_category(value: Any, allowed_categories: list[str] | tuple[str, ...] | None = None) -> str:
    """Normalize a model-provided editorial category without trusting free text."""

    allowed = tuple(str(item).strip() for item in (allowed_categories or ()) if str(item).strip())
    text = clean_text(value)
    if allowed:
        if text in allowed:
            return text
        lowered = text.casefold()
        for candidate in allowed:
            if candidate.casefold() == lowered:
                return candidate
        return "未分类"
    return text or "未分类"


def parse_project_summary_response(data: Any) -> ItemAnalysisResponse:
    """Parse the narrow GitHub summary contract into the existing audit row."""

    raw = _coerce_raw_mapping(data)
    result = _unwrap_response(raw)
    summary = clean_text(result.get("summary_cn") or result.get("introduction") or result.get("intro"))
    sections: list[str] = []
    for label, key in (("主要能力", "capabilities"), ("适用场景", "use_cases")):
        values = clean_string_list(result.get(key))
        if values:
            sections.append(f"{label}：" + "；".join(values[:8]))
    if sections:
        summary = "\n".join([part for part in (summary, *sections) if part])[:8_000]
    if not summary:
        raise ValueError("GitHub project summary is missing summary_cn, capabilities, or use_cases")
    return ItemAnalysisResponse(
        keep=False,
        content_class=PROJECT_TOOL,
        summary_cn=summary,
        reason="github_project_summary",
        risk_flags=clean_string_list(result.get("risk_flags")),
        confidence=0,
        raw_response=raw,
    )


def _coerce_raw_mapping(data: Any) -> dict[str, Any]:
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, str):
        parsed = _parse_json_text(data)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ValueError("Item analysis API response must be a JSON object")


def _unwrap_response(data: dict[str, Any]) -> dict[str, Any]:
    """Handle direct generic JSON and common OpenAI-compatible envelopes."""

    for key in ("result", "data", "output", "response"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, Mapping):
            return dict(value)
        if key == "output" and isinstance(value, list):
            text = _responses_output_text(value)
            if text is None:
                raise ValueError("Item analysis Responses response has no output text")
            parsed = _parse_json_text(text)
            if not isinstance(parsed, Mapping):
                raise ValueError("Item analysis Responses output must be a JSON object")
            return dict(parsed)
        if isinstance(value, str):
            parsed = _parse_json_text(value)
            if isinstance(parsed, Mapping):
                return dict(parsed)
            raise ValueError("Item analysis result must be a JSON object")
        raise ValueError("Item analysis result must be a JSON object")

    if "choices" in data:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("Item analysis OpenAI response has no choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("Item analysis choices[0] must be an object")
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
        text = content_to_text(content)
        if text is None:
            raise ValueError("Item analysis OpenAI response has no JSON content")
        parsed = _parse_json_text(text)
        if not isinstance(parsed, Mapping):
            raise ValueError("Item analysis OpenAI content must be a JSON object")
        return dict(parsed)

    # Some OpenAI-compatible gateways expose an output_text field.
    if "output_text" in data:
        text = content_to_text(data.get("output_text"))
        if text is None:
            raise ValueError("Item analysis output_text is empty")
        parsed = _parse_json_text(text)
        if not isinstance(parsed, Mapping):
            raise ValueError("Item analysis output_text must be a JSON object")
        return dict(parsed)

    return data


def _responses_output_text(value: list[Any]) -> str | None:
    """Extract text from the Responses API's heterogeneous ``output`` list."""

    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
            continue
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("text"), str):
                    parts.append(part["text"])
    text = "".join(parts).strip()
    return text or None


def content_to_text(value: Any) -> str | None:
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
    text = strip_json_fence(value)
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Item analysis API returned invalid JSON content") from exc


def strip_json_fence(value: str) -> str:
    """Remove a Markdown JSON fence emitted by some OpenAI-compatible models."""

    text = value.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def normalize_content_class(value: Any) -> ContentClass | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in CONTENT_CLASSES:
        return text  # type: ignore[return-value]
    return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def clean_string_list(value: Any) -> list[str]:
    """Normalize list-or-string model output while preserving order."""

    if value is None:
        return []
    if isinstance(value, str):
        raw_values: list[Any] = re.split(r"[\r\n,，;；]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        # A scalar/object is not a valid list representation; do not publish
        # its Python repr as if it were a risk statement.
        raw_values = []

    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, (str, int, float, bool)):
            continue
        text = clean_text(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "keep", "保留", "是"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "drop", "reject", "否"}:
            return False
    return default


def clamp_int(value: Any, *, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            number = int(value)
        elif isinstance(value, str) and not value.strip():
            number = default
        else:
            number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(0, min(number, 100))


__all__ = [
    "COMMUNITY_SOCIAL",
    "CONTENT_CLASSES",
    "ContentClass",
    "ITEM_ANALYSIS_RESPONSE_SCHEMA",
    "PROJECT_SUMMARY_RESPONSE_SCHEMA",
    "ItemAnalysisRequest",
    "ItemAnalysisResponse",
    "OFFICIAL_MODEL_COMPANY",
    "PROJECT_TOOL",
    "apply_local_guard",
    "clean_string_list",
    "clean_text",
    "clamp_int",
    "coerce_bool",
    "content_to_text",
    "guard_item_analysis_response",
    "normalize_content_class",
    "parse_item_analysis_response",
    "parse_project_summary_response",
    "strip_json_fence",
    "PaperSupport",
    "RawIntelEnvelope",
    "TriageResult",
    "TriageScores",
    "normalize_topic",
    "normalize_html",
    "normalize_text",
    "parse_triage_response",
    "parse_triage_result",
    "strict_parse_triage",
    "apply_deterministic_guards",
]


# The triage contract lives in ``app.ai.skills.intel_triage`` so it remains
# independent from the legacy item-analysis schema.  Lazy aliases keep the
# common ``from app.ai.schemas import RawIntelEnvelope`` import working without
# introducing a module-import cycle.
_TRIAGE_EXPORTS = {
    "PaperSupport",
    "RawIntelEnvelope",
    "TriageResult",
    "TriageScores",
    "normalize_topic",
    "normalize_html",
    "normalize_text",
    "parse_triage_response",
    "parse_triage_result",
    "strict_parse_triage",
    "apply_deterministic_guards",
}


def __getattr__(name: str):
    if name in _TRIAGE_EXPORTS:
        from app.ai.skills import intel_triage

        return getattr(intel_triage, name)
    raise AttributeError(name)
