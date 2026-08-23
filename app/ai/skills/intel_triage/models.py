"""Provider-neutral contracts for the two-stage intelligence skill.

The models in this module are transport and persistence agnostic.  Stage A is
the cheap conservative screen; Stage B creates the editorial analysis
projection.  Neither contract contains a ``keep`` or historical-event field:
those decisions belong to the pipeline and Stage C respectively.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Mapping, TypeAlias
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import normalize_html, normalize_text, normalize_url


TOPIC_DEVELOPER_ECOSYSTEM = "developer_ecosystem"
TOPIC_MODEL_RELEASE = "model_release"
TOPIC_PRODUCT_APPLICATION = "product_application"
TOPIC_INDUSTRY_DYNAMICS = "industry_dynamics"
TOPIC_TECHNOLOGY_INSIGHT = "technology_insight"
TOPIC_OUTLOOK_RUMOR = "outlook_rumor"

IntelTopic: TypeAlias = Literal[
    "developer_ecosystem",
    "model_release",
    "product_application",
    "industry_dynamics",
    "technology_insight",
    "outlook_rumor",
]
INTEL_TOPICS: tuple[IntelTopic, ...] = (
    TOPIC_DEVELOPER_ECOSYSTEM,
    TOPIC_MODEL_RELEASE,
    TOPIC_PRODUCT_APPLICATION,
    TOPIC_INDUSTRY_DYNAMICS,
    TOPIC_TECHNOLOGY_INSIGHT,
    TOPIC_OUTLOOK_RUMOR,
)
INTEL_TOPIC_LABELS: dict[str, str] = {
    TOPIC_DEVELOPER_ECOSYSTEM: "开发生态",
    TOPIC_MODEL_RELEASE: "模型发布",
    TOPIC_PRODUCT_APPLICATION: "产品应用",
    TOPIC_INDUSTRY_DYNAMICS: "行业动态",
    TOPIC_TECHNOLOGY_INSIGHT: "技术与洞察",
    TOPIC_OUTLOOK_RUMOR: "前瞻与传闻",
}
OFFICIAL_MODEL_COMPANY = "official_model_company"
PROJECT_TOOL = "project_tool"
COMMUNITY_SOCIAL = "community_social"
NEWS_MEDIA = "news_media"
ContentClass: TypeAlias = Literal[
    "official_model_company", "project_tool", "community_social", "news_media"
]
CONTENT_CLASSES: tuple[ContentClass, ...] = (
    OFFICIAL_MODEL_COMPANY, PROJECT_TOOL, COMMUNITY_SOCIAL, NEWS_MEDIA,
)
ENTITY_COMPANY = "company"
ENTITY_PRODUCT = "product"
ENTITY_PERSON = "person"
ENTITY_TECHNOLOGY = "technology"
ENTITY_INDUSTRY_CONCEPT = "industry_concept"
IntelEntityType: TypeAlias = Literal[
    "company", "product", "person", "technology", "industry_concept"
]
ENTITY_TYPES: tuple[IntelEntityType, ...] = (
    ENTITY_COMPANY, ENTITY_PRODUCT, ENTITY_PERSON,
    ENTITY_TECHNOLOGY, ENTITY_INDUSTRY_CONCEPT,
)
def normalize_topic(value: Any) -> IntelTopic | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text if text in INTEL_TOPICS else None  # type: ignore[return-value]


def normalize_content_class(value: Any) -> ContentClass | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text if text in CONTENT_CLASSES else None  # type: ignore[return-value]


def normalize_entity_type(value: Any) -> IntelEntityType | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text if text in ENTITY_TYPES else None  # type: ignore[return-value]


def _clean_list(value: Any, *, limit: int | None = None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: list[Any] = re.split(r"[\r\n,，;；|]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value_item in values:
        if not isinstance(value_item, (str, int, float, bool)):
            continue
        text = normalize_text(str(value_item), preserve_newlines=False)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def _clamp_score(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            number = int(value)
        elif value is None or (isinstance(value, str) and not value.strip()):
            number = default
        else:
            number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(0, min(100, number))


class RawIntelEnvelope(BaseModel):
    """Normalized source item supplied to Stage A or Stage B."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)

    item_id: int | str | None = None
    source_id: str = Field(min_length=1)
    source_name: str | None = None
    source_group: str | None = None
    source_content_class: ContentClass = COMMUNITY_SOCIAL
    external_id: str | None = None
    guid: str | None = None
    content_hash: str | None = None
    title: str = Field(min_length=1)
    url: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    captured_at: datetime | None = None
    summary: str | None = None
    body_text: str | None = None
    raw_html: str | None = None
    language: str | None = None
    kind: str | None = None
    tags: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="python")
            elif hasattr(value, "__dict__"):
                value = vars(value)
            else:
                raise TypeError("RawIntelEnvelope must be a mapping")
        data = dict(value)
        if "source_id" not in data or not str(data.get("source_id") or "").strip():
            raise ValueError("source_id is required")
        title = normalize_text(data.get("title"), preserve_newlines=False)
        if not title:
            raise ValueError("title is required")
        source_class = normalize_content_class(data.get("source_content_class", COMMUNITY_SOCIAL))
        if source_class is None:
            raise ValueError("source_content_class is not supported")
        data["source_content_class"] = source_class
        data["title"] = title
        if data.get("body_text") is not None:
            body_value = data.get("body_text")
            data["body_text"] = (normalize_html(body_value) if isinstance(body_value, str) and "<" in body_value else normalize_text(body_value)) or None
        if data.get("summary") is not None:
            summary_value = data.get("summary")
            data["summary"] = (normalize_html(summary_value) if isinstance(summary_value, str) and "<" in summary_value else normalize_text(summary_value)) or None
        if not isinstance(data.get("metrics", {}), Mapping):
            raise TypeError("metrics must be a mapping")
        if not isinstance(data.get("raw_payload", {}), Mapping):
            raise TypeError("raw_payload must be a mapping")
        data["metrics"] = dict(data.get("metrics") or {})
        data["raw_payload"] = dict(data.get("raw_payload") or {})
        return data

    @field_validator("source_name", "source_group", "external_id", "guid", "content_hash", "author", "language", "kind", mode="before")
    @classmethod
    def _clean_optional_strings(cls, value: Any) -> str | None:
        text = normalize_text(value, preserve_newlines=False)
        return text or None

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: Any) -> str | None:
        return normalize_url(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> list[str]:
        return _clean_list(value, limit=64)

    @property
    def text(self) -> str:
        return "\n\n".join(part for part in (self.title, self.summary, self.body_text) if part)

    def to_provider_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=False)


class IntelEntity(BaseModel):
    """A typed entity extracted by Stage B."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    type: IntelEntityType
    aliases: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity must be an object")
        data = dict(value)
        entity_type = normalize_entity_type(data.get("type"))
        if entity_type is None:
            raise ValueError("entity type must be company, product, person, technology, or industry_concept")
        data["type"] = entity_type
        data["name"] = normalize_text(data.get("name"), preserve_newlines=False)
        data["aliases"] = _clean_list(data.get("aliases"), limit=16)
        return data

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> IntelEntityType:
        normalized = normalize_entity_type(value)
        if normalized is None:
            raise ValueError("entity type must be company, product, person, technology, or industry_concept")
        return normalized

class ScoreComponents(BaseModel):
    """B1 content-value components, each represented on the 0–100 scale."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    audience_relevance: int = Field(default=0, ge=0, le=100, description="0–100 的整数分数")
    material_change: int = Field(default=0, ge=0, le=100, description="0–100 的整数分数")
    impact_scope: int = Field(default=0, ge=0, le=100, description="0–100 的整数分数")
    independent_news_value: int = Field(default=0, ge=0, le=100, description="0–100 的整数分数")
    specificity: int = Field(default=0, ge=0, le=100, description="0–100 的整数分数")

    @field_validator("audience_relevance", "material_change", "impact_scope", "independent_news_value", "specificity", mode="before")
    @classmethod
    def _clamp_fields(cls, value: Any) -> int:
        return _clamp_score(value)

class ScreenResult(BaseModel):
    """Strict Stage A response and auditable failure projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    item_id: int | str | None = None
    decision: Literal["pass", "reject", "uncertain"] = "uncertain"
    reason_code: str = ""
    reason: str = ""
    confidence: int = 0
    risk_flags: list[str] = Field(default_factory=list)
    status: Literal["success", "screen_failed"] = "success"
    error_code: str | None = None
    error_message: str | None = None
    raw_response: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("ScreenResult must be a mapping")
        data = dict(value)
        data["decision"] = _normalize_decision(data.get("decision"))
        data["reason_code"] = normalize_text(data.get("reason_code"), preserve_newlines=False)
        data["reason"] = normalize_text(data.get("reason"), preserve_newlines=False)
        data["risk_flags"] = _clean_list(data.get("risk_flags"), limit=32)
        data["confidence"] = _clamp_score(data.get("confidence"))
        return data

    @field_validator("decision", mode="before")
    @classmethod
    def _validate_decision(cls, value: Any) -> str:
        return _normalize_decision(value)

    @field_validator("reason_code", "reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: Any) -> str:
        return normalize_text(value, preserve_newlines=False)

    @field_validator("risk_flags", mode="before")
    @classmethod
    def _clean_risks(cls, value: Any) -> list[str]:
        return _clean_list(value, limit=32)

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> int:
        return _clamp_score(value)

    def with_item(self, envelope: RawIntelEnvelope) -> "ScreenResult":
        if self.item_id is None and envelope.item_id is not None:
            return self.model_copy(update={"item_id": envelope.item_id})
        return self


class AnalysisResult(BaseModel):
    """Minimal Stage B projection consumed directly by Stage C."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    item_id: int | str | None = None
    topic: IntelTopic = TOPIC_TECHNOLOGY_INSIGHT
    topics: list[IntelTopic] = Field(default_factory=list)
    summary_cn: str = ""
    keywords: list[str] = Field(default_factory=list)
    entities: list[IntelEntity] = Field(default_factory=list)
    b1_priority: int = Field(default=0, ge=0, le=100, description="0–100 的整数分数")
    score_components: ScoreComponents = Field(default_factory=ScoreComponents)
    status: Literal["success", "analysis_failed"] = "success"
    error_code: str | None = None
    error_message: str | None = None
    raw_response: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("AnalysisResult must be a mapping")
        data = dict(value)
        topic_values = data.get("topics")
        topic_value = data.get("topic")
        normalized_topic = normalize_topic(topic_value)
        if normalized_topic is None:
            raise ValueError("topic must be one of: " + ", ".join(INTEL_TOPICS))
        normalized_topics: list[IntelTopic] = [normalized_topic]
        if isinstance(topic_values, (list, tuple, set)):
            for raw_topic in topic_values:
                normalized = normalize_topic(raw_topic)
                if normalized is not None and normalized not in normalized_topics:
                    normalized_topics.append(normalized)
        data["topic"] = normalized_topic
        data["topics"] = normalized_topics
        data["summary_cn"] = normalize_text(data.get("summary_cn"), preserve_newlines=False)
        data["keywords"] = _clean_list(data.get("keywords"), limit=4)
        data["entities"] = data.get("entities") or []
        data["b1_priority"] = _clamp_score(data.get("b1_priority", 0))
        return data

    @field_validator("topic", mode="before")
    @classmethod
    def _validate_topic(cls, value: Any) -> IntelTopic:
        normalized = normalize_topic(value)
        if normalized is None:
            raise ValueError("topic must be one of: " + ", ".join(INTEL_TOPICS))
        return normalized

    @field_validator("summary_cn", mode="before")
    @classmethod
    def _clean_text_fields(cls, value: Any) -> str:
        return normalize_text(value, preserve_newlines=False)

    @field_validator("keywords", mode="before")
    @classmethod
    def _clean_lists(cls, value: Any) -> list[str]:
        return _clean_list(value, limit=4)

    @field_validator("b1_priority", mode="before")
    @classmethod
    def _clamp_scores(cls, value: Any) -> int:
        return _clamp_score(value)

    def with_item(self, envelope: RawIntelEnvelope) -> "AnalysisResult":
        updates: dict[str, Any] = {}
        if self.item_id is None and envelope.item_id is not None:
            updates["item_id"] = envelope.item_id
        return self.model_copy(update=updates) if updates else self

def _normalize_decision(value: Any) -> Literal["pass", "reject", "uncertain"]:
    text = str(value or "uncertain").strip().casefold()
    if text not in {"pass", "reject", "uncertain"}:
        raise ValueError("decision must be pass, reject, or uncertain")
    return text  # type: ignore[return-value]


__all__ = [
    "AnalysisResult", "COMMUNITY_SOCIAL", "CONTENT_CLASSES",
    "ContentClass", "ENTITY_COMPANY", "ENTITY_INDUSTRY_CONCEPT", "ENTITY_PERSON", "ENTITY_PRODUCT",
    "ENTITY_TECHNOLOGY", "ENTITY_TYPES", "IntelEntity", "IntelEntityType", "IntelTopic",
    "INTEL_TOPIC_LABELS", "INTEL_TOPICS", "NEWS_MEDIA", "OFFICIAL_MODEL_COMPANY",
    "PROJECT_TOOL", "RawIntelEnvelope", "ScoreComponents",
    "ScreenResult", "TOPIC_DEVELOPER_ECOSYSTEM", "TOPIC_INDUSTRY_DYNAMICS", "TOPIC_MODEL_RELEASE",
    "TOPIC_OUTLOOK_RUMOR", "TOPIC_PRODUCT_APPLICATION", "TOPIC_TECHNOLOGY_INSIGHT", "normalize_content_class",
    "normalize_entity_type", "normalize_topic",
]
