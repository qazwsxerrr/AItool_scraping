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
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import normalize_html, normalize_text, normalize_url


TOPIC_MODEL = "model"
TOPIC_PRODUCT = "product"
TOPIC_PROJECT = "project"
TOPIC_INDUSTRY = "industry"
TOPIC_TUTORIAL = "tutorial"
TOPIC_OPINION = "opinion"
TOPIC_PAPER = "paper"

IntelTopic: TypeAlias = Literal[
    "model", "product", "project", "industry", "tutorial", "opinion", "paper"
]
INTEL_TOPICS: tuple[IntelTopic, ...] = (
    TOPIC_MODEL, TOPIC_PRODUCT, TOPIC_PROJECT, TOPIC_INDUSTRY,
    TOPIC_TUTORIAL, TOPIC_OPINION, TOPIC_PAPER,
)
SEVEN_TOPIC_TAXONOMY = INTEL_TOPICS
INTEL_TOPIC_LABELS: dict[str, str] = {
    TOPIC_MODEL: "模型", TOPIC_PRODUCT: "产品", TOPIC_PROJECT: "项目",
    TOPIC_INDUSTRY: "行业", TOPIC_TUTORIAL: "教程", TOPIC_OPINION: "观点",
    TOPIC_PAPER: "论文",
}
_TOPIC_ALIASES: dict[str, IntelTopic] = {
    **{topic: topic for topic in INTEL_TOPICS},
    **{label: topic for topic, label in INTEL_TOPIC_LABELS.items()},
    "models": TOPIC_MODEL, "model_release": TOPIC_MODEL, "model_product": TOPIC_MODEL,
    "tool": TOPIC_PRODUCT, "tools": TOPIC_PRODUCT, "app": TOPIC_PRODUCT,
    "repo": TOPIC_PROJECT, "repository": TOPIC_PROJECT, "open_source": TOPIC_PROJECT,
    "industry_infrastructure": TOPIC_INDUSTRY, "research": TOPIC_PAPER,
    "paper/research": TOPIC_PAPER, "papers": TOPIC_PAPER, "guide": TOPIC_TUTORIAL,
    "how_to": TOPIC_TUTORIAL, "how-to": TOPIC_TUTORIAL, "analysis": TOPIC_OPINION,
    "commentary": TOPIC_OPINION,
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
CONTENT_CLASS_TO_DEFAULT_TOPIC: dict[str, IntelTopic] = {
    OFFICIAL_MODEL_COMPANY: TOPIC_PRODUCT,
    PROJECT_TOOL: TOPIC_PROJECT,
    COMMUNITY_SOCIAL: TOPIC_OPINION,
    NEWS_MEDIA: TOPIC_INDUSTRY,
}

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
_ENTITY_TYPE_ALIASES: dict[str, IntelEntityType] = {
    **{kind: kind for kind in ENTITY_TYPES},
    "公司": ENTITY_COMPANY, "企业": ENTITY_COMPANY, "产品": ENTITY_PRODUCT,
    "人物": ENTITY_PERSON, "人": ENTITY_PERSON, "技术": ENTITY_TECHNOLOGY,
    "行业": ENTITY_INDUSTRY_CONCEPT, "行业概念": ENTITY_INDUSTRY_CONCEPT,
    "concept": ENTITY_INDUSTRY_CONCEPT,
}

PAPER_SUPPORT_LEVELS: tuple[str, ...] = ("none", "weak", "supported", "strong")
PaperSupportLevel: TypeAlias = Literal["none", "weak", "supported", "strong"]


def normalize_topic(value: Any) -> IntelTopic | None:
    if not isinstance(value, str):
        return None
    return _TOPIC_ALIASES.get(value.strip().casefold().replace(" ", "_"))


def normalize_content_class(value: Any) -> ContentClass | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace("-", "_")
    if text in CONTENT_CLASSES:
        return text  # type: ignore[return-value]
    return {
        "official": OFFICIAL_MODEL_COMPANY, "official_model": OFFICIAL_MODEL_COMPANY,
        "company": OFFICIAL_MODEL_COMPANY, "project": PROJECT_TOOL, "tool": PROJECT_TOOL,
        "community": COMMUNITY_SOCIAL, "social": COMMUNITY_SOCIAL,
        "media": NEWS_MEDIA, "news": NEWS_MEDIA,
    }.get(text)  # type: ignore[return-value]


def normalize_entity_type(value: Any) -> IntelEntityType | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace("-", "_").replace(" ", "_")
    return _ENTITY_TYPE_ALIASES.get(text)


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

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, str_strip_whitespace=False)

    item_id: int | str | None = Field(default=None, validation_alias=AliasChoices("item_id", "id", "raw_item_id", "normalized_item_id"))
    source_id: str = Field(min_length=1)
    source_name: str | None = None
    source_group: str | None = None
    source_subtype: str | None = None
    source_role: str | None = None
    source_tier: str | None = None
    source_content_class: ContentClass = Field(default=COMMUNITY_SOCIAL, validation_alias=AliasChoices("source_content_class", "content_class", "source_class"))
    external_id: str | None = None
    guid: str | None = None
    content_hash: str | None = None
    title: str = Field(min_length=1)
    url: str | None = Field(default=None, validation_alias=AliasChoices("url", "link", "canonical_url"))
    author: str | None = None
    published_at: datetime | None = None
    captured_at: datetime | None = Field(default=None, validation_alias=AliasChoices("captured_at", "fetched_at"))
    summary: str | None = Field(default=None, validation_alias=AliasChoices("summary", "raw_summary", "description"))
    body_text: str | None = Field(default=None, validation_alias=AliasChoices("body_text", "body", "content", "raw_content", "body_preview"))
    raw_html: str | None = Field(default=None, validation_alias=AliasChoices("raw_html", "html", "content_html"))
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
        if not data.get("body_text"):
            for key in ("body", "content", "raw_content", "body_preview", "summary", "raw_summary", "description", "raw_html", "html", "content_html"):
                if data.get(key):
                    data["body_text"] = data[key]
                    break
        if "summary" not in data and data.get("raw_summary") is not None:
            data["summary"] = data.get("raw_summary")
        if "url" not in data and data.get("link") is not None:
            data["url"] = data.get("link")
        if "source_content_class" not in data:
            data["source_content_class"] = data.get("content_class", COMMUNITY_SOCIAL)
        source_class = normalize_content_class(data.get("source_content_class"))
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
        if data.get("raw_html") is None and data.get("content_html") is not None:
            data["raw_html"] = str(data["content_html"])
        if not isinstance(data.get("metrics", {}), Mapping):
            raise TypeError("metrics must be a mapping")
        if not isinstance(data.get("raw_payload", {}), Mapping):
            raise TypeError("raw_payload must be a mapping")
        data["metrics"] = dict(data.get("metrics") or {})
        data["raw_payload"] = dict(data.get("raw_payload") or {})
        for alias in ("id", "raw_item_id", "normalized_item_id", "link", "canonical_url", "source_class", "content_class", "fetched_at", "raw_summary", "description", "body", "content", "raw_content", "body_preview", "html", "content_html"):
            data.pop(alias, None)
        return data

    @field_validator("source_name", "source_group", "source_subtype", "source_role", "source_tier", "external_id", "guid", "content_hash", "author", "language", "kind", mode="before")
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
    def canonical_url(self) -> str | None:
        return normalize_url(self.url)

    @property
    def content_class(self) -> ContentClass:
        return self.source_content_class

    @property
    def link(self) -> str | None:
        return self.url

    @property
    def body(self) -> str | None:
        return self.body_text

    @property
    def text(self) -> str:
        return "\n\n".join(part for part in (self.title, self.summary, self.body_text) if part)

    def to_provider_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=False)


class PaperSupport(BaseModel):
    """Explicit paper evidence used by the deterministic Stage B guard."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    is_paper: bool = Field(default=False, validation_alias=AliasChoices("is_paper", "paper"))
    support_level: PaperSupportLevel = Field(default="none", validation_alias=AliasChoices("support_level", "level", "paper_support_level"))
    supported: bool = Field(default=False, validation_alias=AliasChoices("supported", "is_supported", "eligible", "hard_gate_pass"))
    source_type: str = Field(default="unknown", validation_alias=AliasChoices("source_type", "source", "origin", "paper_source"))
    paper_url: str | None = Field(default=None, validation_alias=AliasChoices("paper_url", "paper_link", "url", "arxiv_url"))
    evidence_url: str | None = Field(default=None, validation_alias=AliasChoices("evidence_url", "support_url", "official_url", "official_x_url", "community_url", "code_url", "github_url"))
    evidence_type: str | None = Field(default=None, validation_alias=AliasChoices("evidence_type", "support_type"))
    has_official_source: bool = Field(default=False, validation_alias=AliasChoices("has_official_source", "official"))
    has_code: bool = Field(default=False, validation_alias=AliasChoices("has_code", "code_available"))
    arxiv_only: bool = Field(default=False, validation_alias=AliasChoices("arxiv_only", "only_arxiv"))
    support_score: int = 0
    evidence_links: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("paper_support must be a mapping")
        data = dict(value)
        paper_url = data.get("paper_url") or data.get("paper_link") or data.get("url") or data.get("arxiv_url")
        evidence_url = data.get("evidence_url") or data.get("support_url") or data.get("official_url") or data.get("official_x_url") or data.get("community_url") or data.get("code_url") or data.get("github_url")
        if paper_url is not None:
            data["paper_url"] = normalize_url(paper_url)
        if evidence_url is not None:
            data["evidence_url"] = normalize_url(evidence_url)
        if "is_paper" not in data and "paper" in data:
            data["is_paper"] = data.get("paper")
        if "support_level" not in data:
            data["support_level"] = data.get("level", data.get("paper_support_level"))
        if "supported" not in data:
            data["supported"] = data.get("is_supported", data.get("eligible", data.get("hard_gate_pass")))
        if "source_type" not in data:
            data["source_type"] = data.get("source") or data.get("origin") or data.get("paper_source")
        if "evidence_type" not in data:
            data["evidence_type"] = data.get("support_type")
        if "arxiv_only" not in data and isinstance(paper_url, str):
            data["arxiv_only"] = _is_arxiv_url(paper_url) and not evidence_url
        if "has_code" not in data:
            data["has_code"] = bool(data.get("code_available") or data.get("code_url") or data.get("github_url"))
        if "has_official_source" not in data:
            data["has_official_source"] = bool(data.get("official") or data.get("official_url"))
        if not data.get("support_level"):
            data["support_level"] = "supported" if data.get("supported") or data.get("is_supported") or data.get("eligible") else "none"
        if "supported" not in data or data.get("supported") is None:
            data["supported"] = data.get("is_supported") or data.get("support_level") in {"supported", "strong"}
        for bool_field in ("is_paper", "supported", "has_code", "has_official_source", "arxiv_only"):
            data[bool_field] = _coerce_bool(data.get(bool_field), False)
        data["support_score"] = _clamp_score(data.get("support_score"), 0)
        data["evidence_links"] = [normalized for link in (data.get("evidence_links") or []) if (normalized := normalize_url(link))] if isinstance(data.get("evidence_links"), (list, tuple, set)) else []
        if data.get("notes") is not None:
            data["notes"] = normalize_text(data.get("notes"), preserve_newlines=False) or None
        for alias in ("paper", "level", "paper_support_level", "eligible", "is_supported", "hard_gate_pass", "source", "origin", "paper_source", "url", "paper_link", "arxiv_url", "support_url", "official_url", "official_x_url", "community_url", "code_url", "github_url", "support_type", "official", "code_available", "only_arxiv"):
            data.pop(alias, None)
        return data

    @field_validator("support_level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> PaperSupportLevel:
        text = str(value or "none").strip().casefold().replace("-", "_")
        text = {"": "none", "no": "none", "false": "none", "weak_support": "weak", "pass": "supported", "true": "supported", "strong_support": "strong"}.get(text, text)
        if text not in PAPER_SUPPORT_LEVELS:
            raise ValueError("support_level must be none, weak, supported, or strong")
        return text  # type: ignore[return-value]

    @field_validator("source_type", "evidence_type", mode="before")
    @classmethod
    def _normalize_tokens(cls, value: Any) -> str | None:
        if value is None:
            return "unknown"
        text = normalize_text(value, preserve_newlines=False)
        return text.casefold().replace(" ", "_") if text else None

    @field_validator("paper_url", "evidence_url", mode="before")
    @classmethod
    def _normalize_links(cls, value: Any) -> str | None:
        return normalize_url(value)

    @property
    def hard_gate_pass(self) -> bool:
        if not self.is_paper:
            return True
        if self.arxiv_only or not self.supported:
            return False
        if self.support_level not in {"supported", "strong"}:
            return False
        return bool(self.evidence_url or self.has_code or self.has_official_source)

    @property
    def paper_support_ok(self) -> bool:
        return self.hard_gate_pass


class IntelEntity(BaseModel):
    """A typed entity extracted by Stage B."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    name: str = Field(min_length=1)
    type: IntelEntityType = Field(validation_alias=AliasChoices("type", "entity_type", "kind"))
    aliases: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("entity must be an object")
        data = dict(value)
        if "type" not in data:
            data["type"] = data.get("entity_type", data.get("kind"))
        entity_type = normalize_entity_type(data.get("type"))
        if entity_type is None:
            raise ValueError("entity type must be company, product, person, technology, or industry_concept")
        data["type"] = entity_type
        data["name"] = normalize_text(data.get("name"), preserve_newlines=False)
        data["aliases"] = _clean_list(data.get("aliases"), limit=16)
        data.pop("entity_type", None)
        data.pop("kind", None)
        return data

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> IntelEntityType:
        normalized = normalize_entity_type(value)
        if normalized is None:
            raise ValueError("entity type must be company, product, person, technology, or industry_concept")
        return normalized

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    @property
    def entity_type(self) -> IntelEntityType:
        return self.type


class ScoreComponents(BaseModel):
    """Bounded Stage B score components; no historical/event decision lives here."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    relevance: int = 0
    impact: int = 0
    freshness: int = Field(default=0, validation_alias=AliasChoices("freshness", "timeliness", "recency"))
    source_authority: int = Field(default=0, validation_alias=AliasChoices("source_authority", "authority"))
    actionability: int = 0
    total: int = Field(default=0, validation_alias=AliasChoices("total", "total_score", "selection_score", "score"))

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return {"total": value}
        data = dict(value)
        for canonical, aliases in {"freshness": ("timeliness", "recency"), "source_authority": ("authority",), "total": ("total_score", "selection_score", "score")}.items():
            if canonical not in data:
                for alias in aliases:
                    if alias in data:
                        data[canonical] = data[alias]
                        break
        for alias in ("timeliness", "recency", "authority", "total_score", "selection_score", "score"):
            data.pop(alias, None)
        return data

    @field_validator("relevance", "impact", "freshness", "source_authority", "actionability", "total", mode="before")
    @classmethod
    def _clamp_fields(cls, value: Any) -> int:
        return _clamp_score(value)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class ScreenResult(BaseModel):
    """Strict Stage A response and auditable failure projection."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
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
        if "item_id" not in data:
            data["item_id"] = data.get("id", data.get("raw_item_id"))
        data["decision"] = _normalize_decision(data.get("decision"))
        data["reason_code"] = normalize_text(data.get("reason_code") or data.get("code"), preserve_newlines=False)
        data["reason"] = normalize_text(data.get("reason") or data.get("explanation"), preserve_newlines=False)
        data["risk_flags"] = _clean_list(data.get("risk_flags") or data.get("risks"), limit=32)
        data["confidence"] = _clamp_score(data.get("confidence"))
        for alias in ("id", "raw_item_id", "code", "explanation", "risks"):
            data.pop(alias, None)
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
    """Strict Stage B analysis projection and auditable failure record."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    item_id: int | str | None = None
    topic: IntelTopic = TOPIC_OPINION
    topics: list[IntelTopic] = Field(default_factory=list)
    summary_cn: str = ""
    keywords: list[str] = Field(default_factory=list)
    entities: list[IntelEntity] = Field(default_factory=list)
    selection_score: int = Field(default=0, validation_alias=AliasChoices("selection_score", "score", "display_score", "total_score"))
    score_components: ScoreComponents = Field(default_factory=ScoreComponents, validation_alias=AliasChoices("score_components", "scores", "score_breakdown"))
    paper_support: PaperSupport = Field(default_factory=PaperSupport)
    risk_flags: list[str] = Field(default_factory=list)
    reason: str = ""
    confidence: int = 0
    source_content_class: ContentClass | None = None
    source_group: str | None = None
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
        if "item_id" not in data:
            data["item_id"] = data.get("id", data.get("raw_item_id"))
        topic_values = data.get("topics") or data.get("topic_labels")
        topic_value = data.get("topic")
        if isinstance(topic_value, (list, tuple, set)):
            topic_values = topic_values or topic_value
            topic_value = next(iter(topic_value), None)
        if topic_value is None and topic_values:
            topic_value = next(iter(topic_values), None) if isinstance(topic_values, (list, tuple, set)) else topic_values
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
        data["summary_cn"] = normalize_text(data.get("summary_cn") or data.get("summary"), preserve_newlines=False)
        data["keywords"] = _clean_list(data.get("keywords") or data.get("key_terms") or data.get("tags"), limit=48)
        data["entities"] = data.get("entities") or data.get("typed_entities") or []
        data["selection_score"] = _clamp_score(data.get("selection_score", data.get("score", data.get("display_score", data.get("total_score", 0)))))
        data["risk_flags"] = _clean_list(data.get("risk_flags") or data.get("risks") or data.get("risk"), limit=32)
        data["reason"] = normalize_text(data.get("reason"), preserve_newlines=False)
        data["confidence"] = _clamp_score(data.get("confidence"))
        if "source_content_class" not in data and data.get("content_class") is not None:
            data["source_content_class"] = data.get("content_class")
        if data.get("source_group") is not None:
            data["source_group"] = normalize_text(data.get("source_group"), preserve_newlines=False) or None
        for alias in ("id", "raw_item_id", "topic_labels", "summary", "key_terms", "tags", "score", "display_score", "total_score", "typed_entities", "risks", "risk", "content_class"):
            data.pop(alias, None)
        return data

    @field_validator("topic", mode="before")
    @classmethod
    def _validate_topic(cls, value: Any) -> IntelTopic:
        normalized = normalize_topic(value)
        if normalized is None:
            raise ValueError("topic must be one of: " + ", ".join(INTEL_TOPICS))
        return normalized

    @field_validator("summary_cn", "reason", mode="before")
    @classmethod
    def _clean_text_fields(cls, value: Any) -> str:
        return normalize_text(value, preserve_newlines=False)

    @field_validator("keywords", "risk_flags", mode="before")
    @classmethod
    def _clean_lists(cls, value: Any) -> list[str]:
        return _clean_list(value, limit=48)

    @field_validator("selection_score", "confidence", mode="before")
    @classmethod
    def _clamp_scores(cls, value: Any) -> int:
        return _clamp_score(value)

    @field_validator("source_content_class", mode="before")
    @classmethod
    def _normalize_source_class(cls, value: Any) -> ContentClass | None:
        if value is None:
            return None
        normalized = normalize_content_class(value)
        if normalized is None:
            raise ValueError("source_content_class is not supported")
        return normalized

    def with_item(self, envelope: RawIntelEnvelope) -> "AnalysisResult":
        updates: dict[str, Any] = {}
        if self.item_id is None and envelope.item_id is not None:
            updates["item_id"] = envelope.item_id
        if self.source_content_class is None:
            updates["source_content_class"] = envelope.source_content_class
        if self.source_group is None and envelope.source_group:
            updates["source_group"] = envelope.source_group
        return self.model_copy(update=updates) if updates else self

    @property
    def scores(self) -> ScoreComponents:
        return self.score_components

    @property
    def paper_gate_pass(self) -> bool:
        return self.paper_support.hard_gate_pass if self.topic == TOPIC_PAPER else True


def _normalize_decision(value: Any) -> Literal["pass", "reject", "uncertain"]:
    text = str(value or "uncertain").strip().casefold().replace("-", "_")
    text = {
        "accepted": "pass", "approved": "pass", "yes": "pass",
        "拒绝": "reject", "淘汰": "reject", "否": "reject", "maybe": "uncertain",
        "review": "uncertain", "待定": "uncertain",
    }.get(text, text)
    if text not in {"pass", "reject", "uncertain"}:
        raise ValueError("decision must be pass, reject, or uncertain")
    return text  # type: ignore[return-value]


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"true", "1", "yes", "y", "on", "supported", "pass", "是"}:
            return True
        if text in {"false", "0", "no", "n", "off", "reject", "否"}:
            return False
    return default


def _is_arxiv_url(value: str) -> bool:
    try:
        host = (urlsplit(value).hostname or "").casefold()
    except ValueError:
        return False
    return host in {"arxiv.org", "export.arxiv.org"} or host.endswith(".arxiv.org")


__all__ = [
    "AnalysisResult", "COMMUNITY_SOCIAL", "CONTENT_CLASSES", "CONTENT_CLASS_TO_DEFAULT_TOPIC",
    "ContentClass", "ENTITY_COMPANY", "ENTITY_INDUSTRY_CONCEPT", "ENTITY_PERSON", "ENTITY_PRODUCT",
    "ENTITY_TECHNOLOGY", "ENTITY_TYPES", "IntelEntity", "IntelEntityType", "IntelTopic",
    "INTEL_TOPIC_LABELS", "INTEL_TOPICS", "NEWS_MEDIA", "OFFICIAL_MODEL_COMPANY", "PAPER_SUPPORT_LEVELS",
    "PROJECT_TOOL", "PaperSupport", "PaperSupportLevel", "RawIntelEnvelope", "ScoreComponents",
    "ScreenResult", "SEVEN_TOPIC_TAXONOMY", "TOPIC_INDUSTRY", "TOPIC_MODEL", "TOPIC_OPINION",
    "TOPIC_PAPER", "TOPIC_PRODUCT", "TOPIC_PROJECT", "TOPIC_TUTORIAL", "normalize_content_class",
    "normalize_entity_type", "normalize_topic",
]
