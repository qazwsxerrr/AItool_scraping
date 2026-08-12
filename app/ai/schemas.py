"""Schemas and deterministic parsing rules for one-pass item analysis.

This module contains no network or provider-specific code.  It is the boundary
between untrusted model JSON and the rest of the ingestion pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Generic, Literal, Mapping, TypeAlias, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator


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
    "summary_cn": "string",
    "reason": "string",
    "risk_flags": "array<string>",
    "needs_verification": "boolean",
    "official_url": "string|null",
    "confidence": "integer 0-100",
}

PROJECT_SUMMARY_RESPONSE_SCHEMA: dict[str, str] = {
    "summary_cn": "string",
    "capabilities": "array<string>",
    "use_cases": "array<string>",
    "risk_flags": "array<string>",
}

# V3 daily intelligence contracts.  These schemas deliberately keep the
# deterministic editorial decisions out of the model's remit: source tier,
# primary eligibility, quotas and publication gates are decided locally.
DAILY_SECTIONS: tuple[str, ...] = (
    "model_product",
    "industry_infrastructure",
    "research",
    "open_source_tool",
    "practice_opinion",
)

TRIAGE_RESPONSE_SCHEMA: dict[str, str] = {
    "keep": "boolean",
    "section": "model_product|industry_infrastructure|research|open_source_tool|practice_opinion",
    "event_type": "string",
    "event_hint": "string",
    "entities": "array<string>",
    "impact_score": "integer 0-100",
    "novelty_score": "integer 0-100",
    "readability_score": "integer 0-100",
    "risk_flags": "array<string>",
    "reason": "string",
    "confidence": "integer 0-100",
    "claim_types": "array<string>",
}

CLUSTER_DECISION_VALUES: tuple[str, ...] = ("merge", "related", "separate", "uncertain")
CLUSTER_RESPONSE_SCHEMA: dict[str, str] = {
    "decision": "merge|related|separate|uncertain",
    "confidence": "integer 0-100",
    "reason": "string",
    "canonical_event_hint": "string|null",
}

EVENT_EDITORIAL_RESPONSE_SCHEMA: dict[str, str] = {
    "title": "string",
    "summary_cn": "string",
    "why_it_matters": "string",
    "facts": "array<{text:string,evidence_ids:array<string> non-empty}>",
    "risk_notes": "array<string>",
    "uncertainties": "array<string>",
    "tags": "array<string>",
}


StageStatus: TypeAlias = Literal[
    "success",
    "not_configured",
    "request_error",
    "http_error",
    "invalid_json",
    "parse_error",
]
StageT = TypeVar("StageT")


@dataclass(frozen=True)
class StageCallResult(Generic[StageT]):
    """Auditable result for a daily AI stage call.

    The provider response is retained in ``raw`` even when parsing fails.  A
    stage therefore never turns an invalid model response into an apparently
    successful empty result.  ``raw_response`` is kept as a compatibility
    alias for callers that use the naming of :class:`ItemAnalysisResponse`.
    """

    stage: str
    status: StageStatus
    parsed: StageT | None = None
    raw: Any | None = None
    error: str | None = None
    model: str | None = None

    @property
    def raw_response(self) -> Any | None:
        return self.raw

    @property
    def ok(self) -> bool:
        return self.status == "success" and self.parsed is not None


class TriageResponse(BaseModel):
    """Strict model output for deterministic prefilter/triage input."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    keep: bool
    section: Literal[
        "model_product",
        "industry_infrastructure",
        "research",
        "open_source_tool",
        "practice_opinion",
    ]
    event_type: StrictStr
    event_hint: StrictStr
    entities: list[StrictStr] = Field(default_factory=list)
    impact_score: StrictInt = Field(ge=0, le=100)
    novelty_score: StrictInt = Field(ge=0, le=100)
    readability_score: StrictInt = Field(ge=0, le=100)
    risk_flags: list[StrictStr] = Field(default_factory=list)
    reason: StrictStr
    confidence: StrictInt = Field(ge=0, le=100)
    claim_types: list[StrictStr] = Field(default_factory=list)

    @field_validator("event_type", "event_hint", "reason")
    @classmethod
    def _non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text fields must not be empty")
        return value

    @field_validator("entities", "risk_flags", "claim_types")
    @classmethod
    def _clean_string_lists(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = value.strip()
            if text and text not in seen:
                cleaned.append(text)
                seen.add(text)
        return cleaned


class ClusterDecision(BaseModel):
    """Strict fuzzy-cluster judgement; local code applies the >=80 threshold."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    decision: Literal["merge", "related", "separate", "uncertain"]
    confidence: StrictInt = Field(ge=0, le=100)
    reason: StrictStr
    canonical_event_hint: StrictStr | None = None

    @field_validator("reason")
    @classmethod
    def _non_empty_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be empty")
        return value

    @field_validator("canonical_event_hint")
    @classmethod
    def _clean_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class EditorialFact(BaseModel):
    """One concrete editorial statement and its auditable evidence IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    text: StrictStr
    evidence_ids: list[StrictStr] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _non_empty_fact(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("fact text must not be empty")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def _non_empty_evidence_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = value.strip()
            if text and text not in seen:
                cleaned.append(text)
                seen.add(text)
        if not cleaned:
            raise ValueError("every fact must include at least one evidence_id")
        return cleaned


class EventEditorialResponse(BaseModel):
    """Structured event copy with citation-bearing concrete facts."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    title: StrictStr
    summary_cn: StrictStr
    why_it_matters: StrictStr
    facts: list[EditorialFact] = Field(min_length=1)
    risk_notes: list[StrictStr] = Field(default_factory=list)
    uncertainties: list[StrictStr] = Field(default_factory=list)
    tags: list[StrictStr] = Field(default_factory=list)

    @field_validator("title", "summary_cn", "why_it_matters")
    @classmethod
    def _non_empty_editorial_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("editorial text fields must not be empty")
        return value

    @field_validator("risk_notes", "uncertainties", "tags")
    @classmethod
    def _clean_editorial_lists(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = value.strip()
            if text and text not in seen:
                cleaned.append(text)
                seen.add(text)
        return cleaned


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
    used as a verified fact by this module.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    keep: bool
    content_class: ContentClass
    summary_cn: str
    reason: str
    risk_flags: list[str] = Field(default_factory=list)
    needs_verification: bool
    official_url: str | None = None
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
            keep=coerce_bool(data.get("keep"), default=False),
            needs_verification=coerce_bool(data.get("needs_verification"), default=False),
            summary_cn=clean_text(data.get("summary_cn")),
            reason=clean_text(data.get("reason")),
            risk_flags=clean_string_list(data.get("risk_flags")),
            official_url=clean_url(data.get("official_url")),
            confidence=clamp_int(data.get("confidence")),
        )
        return data


def apply_local_guard(
    response: ItemAnalysisResponse,
    source_content_class: str | None = None,
) -> ItemAnalysisResponse:
    """Apply deterministic safety rules after model parsing.

    When a source category is provided, it is authoritative: the model cannot
    switch an item into a different verification path. Official announcements and
    community/social leads always require an external verification pass. The guard
    never upgrades ``keep`` or confidence and never removes raw provider data.
    """

    final_class = response.content_class
    if source_content_class is not None:
        source_class = normalize_content_class(source_content_class)
        if source_class is None:
            raise ValueError("source_content_class must be one of: " + ", ".join(CONTENT_CLASSES))
        final_class = source_class

    must_verify = final_class in {"official_model_company", "community_social"}
    if final_class != response.content_class or (must_verify and not response.needs_verification):
        return response.model_copy(
            update={
                "content_class": final_class,
                "needs_verification": response.needs_verification or must_verify,
            }
        )
    return response


# More explicit alias for callers that use the phrase from the pipeline docs.
guard_item_analysis_response = apply_local_guard


def parse_item_analysis_response(
    data: Any,
    source_content_class: str,
) -> ItemAnalysisResponse:
    """Unwrap, strictly parse, normalize, and guard a provider response.

    Envelope/JSON failures raise ``ValueError``.  Individual field quirks are
    normalized to safe defaults. The source category is always authoritative;
    the model category remains only in ``raw_response`` for auditing.
    """

    raw = _coerce_raw_mapping(data)
    result = _unwrap_response(raw)
    # The direct-link field is optional, while the classification, summary and
    # risk envelope remains mandatory.
    required_fields = [key for key in ITEM_ANALYSIS_RESPONSE_SCHEMA if key != "official_url"]
    missing = [key for key in required_fields if key not in result]
    if missing:
        raise ValueError("Item analysis response is missing required fields: " + ", ".join(missing))
    fallback_class = normalize_content_class(source_content_class)
    if fallback_class is None:
        raise ValueError("source_content_class must be one of: " + ", ".join(CONTENT_CLASSES))

    response = ItemAnalysisResponse(
        keep=coerce_bool(result.get("keep"), default=False),
        content_class=fallback_class,
        summary_cn=clean_text(result.get("summary_cn")),
        reason=clean_text(result.get("reason")),
        risk_flags=clean_string_list(result.get("risk_flags")),
        needs_verification=coerce_bool(result.get("needs_verification"), default=False),
        official_url=clean_url(result.get("official_url")),
        confidence=clamp_int(result.get("confidence")),
        raw_response=raw,
    )
    return apply_local_guard(response, fallback_class)


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
        needs_verification=False,
        official_url=None,
        confidence=0,
        raw_response=raw,
    )


def parse_triage_response(data: Any) -> TriageResponse:
    """Strictly parse a provider response for the triage stage."""

    raw = _coerce_raw_mapping(data)
    result = _unwrap_response(raw)
    try:
        return TriageResponse.model_validate(result, strict=True)
    except Exception as exc:
        # Keep a stable, auditable public error type while retaining the
        # provider payload in StageCallResult at the client boundary.
        raise ValueError(f"Triage response failed schema validation: {exc}") from exc


def parse_cluster_decision_response(data: Any) -> ClusterDecision:
    """Strictly parse a provider response for fuzzy cluster judgement."""

    raw = _coerce_raw_mapping(data)
    result = _unwrap_response(raw)
    try:
        return ClusterDecision.model_validate(result, strict=True)
    except Exception as exc:
        raise ValueError(f"Cluster decision failed schema validation: {exc}") from exc


def parse_event_editorial_response(data: Any) -> EventEditorialResponse:
    """Strictly parse event copy and reject facts without evidence IDs."""

    raw = _coerce_raw_mapping(data)
    result = _unwrap_response(raw)
    try:
        return EventEditorialResponse.model_validate(result, strict=True)
    except Exception as exc:
        raise ValueError(f"Event editorial response failed schema validation: {exc}") from exc


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


def clean_url(value: Any) -> str | None:
    if value is None:
        return None
    text = clean_text(value)
    if not text or text.lower() in {"null", "none", "n/a", "na", "-"}:
        return None
    try:
        parsed = urlparse(text)
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return text


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
    "DAILY_SECTIONS",
    "CLUSTER_DECISION_VALUES",
    "TRIAGE_RESPONSE_SCHEMA",
    "CLUSTER_RESPONSE_SCHEMA",
    "EVENT_EDITORIAL_RESPONSE_SCHEMA",
    "StageCallResult",
    "StageStatus",
    "TriageResponse",
    "ClusterDecision",
    "EditorialFact",
    "EventEditorialResponse",
    "ITEM_ANALYSIS_RESPONSE_SCHEMA",
    "PROJECT_SUMMARY_RESPONSE_SCHEMA",
    "ItemAnalysisRequest",
    "ItemAnalysisResponse",
    "OFFICIAL_MODEL_COMPANY",
    "PROJECT_TOOL",
    "apply_local_guard",
    "clean_string_list",
    "clean_text",
    "clean_url",
    "clamp_int",
    "coerce_bool",
    "content_to_text",
    "guard_item_analysis_response",
    "normalize_content_class",
    "parse_item_analysis_response",
    "parse_project_summary_response",
    "parse_triage_response",
    "parse_cluster_decision_response",
    "parse_event_editorial_response",
    "strip_json_fence",
]
