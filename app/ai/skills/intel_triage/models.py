"""Pure Pydantic contracts for the AI Intel Triage skill.

The models in this module deliberately contain no HTTP, database, or job
imports.  Provider output is untrusted input, so aliases are normalized in a
``before`` validator and the final models reject unknown fields.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Mapping, TypeAlias
from urllib.parse import urlsplit, urlunsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .normalize import normalize_html, normalize_text, normalize_url


# Seven editorial topics.  Keep the English slugs stable for code and config;
# labels are only presentation metadata.
TOPIC_MODEL = "model"
TOPIC_PRODUCT = "product"
TOPIC_PROJECT = "project"
TOPIC_INDUSTRY = "industry"
TOPIC_TUTORIAL = "tutorial"
TOPIC_OPINION = "opinion"
TOPIC_PAPER = "paper"

IntelTopic: TypeAlias = Literal[
    "model",
    "product",
    "project",
    "industry",
    "tutorial",
    "opinion",
    "paper",
]

INTEL_TOPICS: tuple[IntelTopic, ...] = (
    TOPIC_MODEL,
    TOPIC_PRODUCT,
    TOPIC_PROJECT,
    TOPIC_INDUSTRY,
    TOPIC_TUTORIAL,
    TOPIC_OPINION,
    TOPIC_PAPER,
)
SEVEN_TOPIC_TAXONOMY: tuple[str, ...] = INTEL_TOPICS
INTEL_TOPIC_LABELS: dict[str, str] = {
    TOPIC_MODEL: "模型",
    TOPIC_PRODUCT: "产品",
    TOPIC_PROJECT: "项目",
    TOPIC_INDUSTRY: "行业",
    TOPIC_TUTORIAL: "教程",
    TOPIC_OPINION: "观点",
    TOPIC_PAPER: "论文",
}

_TOPIC_ALIASES: dict[str, IntelTopic] = {
    **{topic: topic for topic in INTEL_TOPICS},
    **{label: topic for topic, label in INTEL_TOPIC_LABELS.items()},
    "models": TOPIC_MODEL,
    "model_release": TOPIC_MODEL,
    "model_product": TOPIC_MODEL,
    "产品发布": TOPIC_PRODUCT,
    "tool": TOPIC_PRODUCT,
    "tools": TOPIC_PRODUCT,
    "app": TOPIC_PRODUCT,
    "repo": TOPIC_PROJECT,
    "repository": TOPIC_PROJECT,
    "open_source": TOPIC_PROJECT,
    "industry_infrastructure": TOPIC_INDUSTRY,
    "research": TOPIC_PAPER,
    "paper/research": TOPIC_PAPER,
    "papers": TOPIC_PAPER,
    "guide": TOPIC_TUTORIAL,
    "how_to": TOPIC_TUTORIAL,
    "how-to": TOPIC_TUTORIAL,
    "analysis": TOPIC_OPINION,
    "commentary": TOPIC_OPINION,
}

OFFICIAL_MODEL_COMPANY = "official_model_company"
PROJECT_TOOL = "project_tool"
COMMUNITY_SOCIAL = "community_social"
ContentClass: TypeAlias = Literal[
    "official_model_company",
    "project_tool",
    "community_social",
]
CONTENT_CLASSES: tuple[ContentClass, ...] = (
    OFFICIAL_MODEL_COMPANY,
    PROJECT_TOOL,
    COMMUNITY_SOCIAL,
)

CONTENT_CLASS_TO_DEFAULT_TOPIC: dict[str, IntelTopic] = {
    OFFICIAL_MODEL_COMPANY: TOPIC_PRODUCT,
    PROJECT_TOOL: TOPIC_PROJECT,
    COMMUNITY_SOCIAL: TOPIC_OPINION,
}

NOVELTY_STATUSES: tuple[str, ...] = ("new", "update", "repeat", "unknown")
NoveltyStatus: TypeAlias = Literal["new", "update", "repeat", "unknown"]
PAPER_SUPPORT_LEVELS: tuple[str, ...] = ("none", "weak", "supported", "strong")
PaperSupportLevel: TypeAlias = Literal["none", "weak", "supported", "strong"]


def normalize_topic(value: Any) -> IntelTopic | None:
    """Normalize an English or Chinese topic label to one stable slug."""

    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace(" ", "_")
    return _TOPIC_ALIASES.get(text)


def normalize_content_class(value: Any) -> ContentClass | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace("-", "_")
    if text in CONTENT_CLASSES:
        return text  # type: ignore[return-value]
    aliases = {
        "official": OFFICIAL_MODEL_COMPANY,
        "official_model": OFFICIAL_MODEL_COMPANY,
        "company": OFFICIAL_MODEL_COMPANY,
        "project": PROJECT_TOOL,
        "tool": PROJECT_TOOL,
        "community": COMMUNITY_SOCIAL,
        "social": COMMUNITY_SOCIAL,
    }
    return aliases.get(text)  # type: ignore[return-value]


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
    for item in values:
        if not isinstance(item, (str, int, float, bool)):
            continue
        text = normalize_text(str(item))
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if limit is not None and len(result) >= limit:
            break
    return result


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"true", "1", "yes", "y", "on", "keep", "是", "保留"}:
            return True
        if text in {"false", "0", "no", "n", "off", "drop", "reject", "否", "拒绝"}:
            return False
    return default


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
    """Normalized raw item passed to a triage provider.

    The aliases accept existing fetch DTOs (``id``, ``link``, ``content`` and
    ``captured_at``) as well as the raw-item vocabulary used by future stages.
    HTML is normalized at the boundary, while ``raw_html`` remains available
    for audit/debugging.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=False,
    )

    item_id: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("item_id", "id", "raw_item_id", "normalized_item_id"),
    )
    source_id: str = Field(min_length=1)
    source_name: str | None = None
    source_group: str | None = None
    source_subtype: str | None = None
    source_role: str | None = None
    source_tier: str | None = None
    source_content_class: ContentClass = Field(
        default=COMMUNITY_SOCIAL,
        validation_alias=AliasChoices("source_content_class", "content_class", "source_class"),
    )
    external_id: str | None = None
    guid: str | None = None
    content_hash: str | None = None
    title: str = Field(min_length=1)
    url: str | None = Field(default=None, validation_alias=AliasChoices("url", "link", "canonical_url"))
    author: str | None = None
    published_at: datetime | None = None
    captured_at: datetime | None = Field(default=None, validation_alias=AliasChoices("captured_at", "fetched_at"))
    summary: str | None = Field(default=None, validation_alias=AliasChoices("summary", "raw_summary", "description"))
    body_text: str | None = Field(
        default=None,
        validation_alias=AliasChoices("body_text", "body", "content", "raw_content", "body_preview"),
    )
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
        if not isinstance(value, Mapping):
            raise TypeError("RawIntelEnvelope must be a mapping")
        data = dict(value)
        if "source_id" not in data or not str(data.get("source_id") or "").strip():
            raise ValueError("source_id is required")
        title = normalize_text(data.get("title"))
        if not title:
            raise ValueError("title is required")

        # Normalize common collector names before Pydantic alias resolution.
        if not data.get("body_text"):
            for key in (
                "body", "content", "raw_content", "body_preview", "summary", "raw_summary",
                "description", "raw_html", "html", "content_html",
            ):
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
            body_text = normalize_html(body_value) if isinstance(body_value, str) and "<" in body_value else normalize_text(body_value)
            data["body_text"] = body_text or None
        if data.get("summary") is not None:
            summary_value = data.get("summary")
            summary_text = normalize_html(summary_value) if isinstance(summary_value, str) and "<" in summary_value else normalize_text(summary_value)
            data["summary"] = summary_text or None
        if data.get("raw_html") is None and data.get("content_html") is not None:
            data["raw_html"] = str(data["content_html"])
        if not isinstance(data.get("metrics", {}), Mapping):
            raise TypeError("metrics must be a mapping")
        if not isinstance(data.get("raw_payload", {}), Mapping):
            raise TypeError("raw_payload must be a mapping")
        data["metrics"] = dict(data.get("metrics") or {})
        data["raw_payload"] = dict(data.get("raw_payload") or {})
        # AliasChoices are ergonomic at the API boundary, but consumed aliases
        # must not remain as duplicate keys when extra="forbid" is enabled.
        for alias in (
            "id", "raw_item_id", "normalized_item_id", "link", "canonical_url",
            "source_class", "content_class", "fetched_at", "raw_summary", "description",
            "body", "content", "raw_content", "body_preview", "html", "content_html",
        ):
            data.pop(alias, None)
        return data

    @field_validator(
        "source_name", "source_group", "source_subtype", "source_role", "source_tier",
        "external_id", "guid", "content_hash", "author", "language", "kind", mode="before",
    )
    @classmethod
    def _clean_optional_strings(cls, value: Any) -> str | None:
        text = normalize_text(value)
        return text or None

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: Any) -> str | None:
        return normalize_url(value)

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
    def content(self) -> str | None:
        return self.body_text

    @property
    def raw_content(self) -> str | None:
        return self.body_text

    @property
    def fetched_at(self) -> datetime | None:
        return self.captured_at

    @property
    def text(self) -> str:
        return "\n\n".join(part for part in (self.title, self.summary, self.body_text) if part)

    def to_provider_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=False)


class PaperSupport(BaseModel):
    """Evidence fields used by the deterministic paper hard gate.

    ``supported`` is intentionally explicit.  A bare arXiv link or a model
    assertion cannot promote a paper into the selected pool.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    is_paper: bool = Field(default=False, validation_alias=AliasChoices("is_paper", "paper"))
    support_level: PaperSupportLevel = Field(
        default="none",
        validation_alias=AliasChoices("support_level", "level", "paper_support_level"),
    )
    supported: bool = Field(default=False, validation_alias=AliasChoices("supported", "is_supported", "eligible", "hard_gate_pass"))
    source_type: str = Field(
        default="unknown",
        validation_alias=AliasChoices("source_type", "source", "origin", "paper_source"),
    )
    paper_url: str | None = Field(default=None, validation_alias=AliasChoices("paper_url", "paper_link", "url", "arxiv_url"))
    evidence_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "evidence_url", "support_url", "official_url", "official_x_url", "community_url",
            "code_url", "github_url",
        ),
    )
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
        if "is_paper" not in data:
            data["is_paper"] = data.get("paper", False)
        if "source_type" not in data:
            data["source_type"] = data.get("source") or data.get("origin") or "unknown"
        paper_url = data.get("paper_url") or data.get("paper_link") or data.get("url") or data.get("arxiv_url")
        evidence_url = (
            data.get("evidence_url")
            or data.get("support_url")
            or data.get("official_url")
            or data.get("official_x_url")
            or data.get("community_url")
            or data.get("code_url")
            or data.get("github_url")
        )
        if paper_url is not None:
            data["paper_url"] = normalize_url(paper_url)
        if evidence_url is not None:
            data["evidence_url"] = normalize_url(evidence_url)
        if "arxiv_only" not in data and isinstance(paper_url, str):
            data["arxiv_only"] = _is_arxiv_url(paper_url) and not evidence_url
        if "has_code" not in data:
            data["has_code"] = bool(data.get("code_available") or data.get("code_url") or data.get("github_url"))
        if "has_official_source" not in data:
            data["has_official_source"] = bool(data.get("official") or data.get("official_url"))
        if "support_level" not in data:
            data["support_level"] = "supported" if data.get("supported") or data.get("is_supported") or data.get("eligible") else "none"
        if "supported" not in data:
            data["supported"] = data.get("is_supported") or data.get("support_level") in {"supported", "strong"}
        for bool_field in ("is_paper", "supported", "has_code", "has_official_source", "arxiv_only"):
            data[bool_field] = _coerce_bool(data.get(bool_field), False)
        if "support_score" in data:
            try:
                data["support_score"] = max(0, min(100, int(float(data["support_score"]))))
            except (TypeError, ValueError, OverflowError):
                data["support_score"] = 0
        if "evidence_links" in data:
            data["evidence_links"] = [normalize_url(link) for link in data["evidence_links"] if normalize_url(link)] if isinstance(data["evidence_links"], (list, tuple, set)) else []
        if data.get("notes") is not None:
            data["notes"] = normalize_text(data.get("notes")) or None
        for alias in (
            "paper", "level", "paper_support_level", "eligible", "is_supported", "hard_gate_pass",
            "source", "origin", "paper_source", "url", "paper_link", "arxiv_url", "support_url",
            "official_url", "official_x_url", "community_url", "code_url", "github_url",
            "support_type", "official", "code_available", "only_arxiv",
        ):
            data.pop(alias, None)
        return data

    @field_validator("support_level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> PaperSupportLevel:
        text = str(value or "none").strip().casefold().replace("-", "_")
        aliases = {
            "": "none",
            "no": "none",
            "false": "none",
            "weak_support": "weak",
            "pass": "supported",
            "true": "supported",
            "strong_support": "strong",
        }
        text = aliases.get(text, text)
        if text not in PAPER_SUPPORT_LEVELS:
            raise ValueError("support_level must be none, weak, supported, or strong")
        return text  # type: ignore[return-value]

    @field_validator("source_type", "evidence_type", mode="before")
    @classmethod
    def _normalize_tokens(cls, value: Any) -> str | None:
        if value is None:
            return "unknown"
        text = normalize_text(value)
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
    def paper_support_url(self) -> str | None:
        return self.evidence_url

    @property
    def paper_support_ok(self) -> bool:
        return self.hard_gate_pass


class TriageScores(BaseModel):
    """Bounded score components returned by a triage provider."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    relevance: int = Field(default=0, validation_alias=AliasChoices("relevance", "relevance_score"))
    novelty: int = Field(default=0, validation_alias=AliasChoices("novelty", "novelty_score"))
    impact: int = Field(default=0, validation_alias=AliasChoices("impact", "impact_score", "heat", "heat_score"))
    actionability: int = Field(default=0, validation_alias=AliasChoices("actionability", "actionability_score"))
    total: int = Field(default=0, validation_alias=AliasChoices("total", "total_score", "selection_score", "score", "display_score"))

    @model_validator(mode="before")
    @classmethod
    def _normalize_scores(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return {"total": value}
        data = dict(value)
        if "total" not in data:
            for alias in ("total_score", "selection_score", "score", "display_score"):
                if alias in data:
                    data["total"] = data[alias]
                    break
        if "impact" not in data:
            for alias in ("impact_score", "heat", "heat_score"):
                if alias in data:
                    data["impact"] = data[alias]
                    break
        if "relevance" not in data and "relevance_score" in data:
            data["relevance"] = data["relevance_score"]
        if "novelty" not in data and "novelty_score" in data:
            data["novelty"] = data["novelty_score"]
        if "actionability" not in data and "actionability_score" in data:
            data["actionability"] = data["actionability_score"]
        for alias in (
            "relevance_score", "novelty_score", "impact_score", "heat", "heat_score",
            "actionability_score", "total_score", "selection_score", "score", "display_score",
        ):
            data.pop(alias, None)
        return data

    @field_validator("relevance", "novelty", "impact", "actionability", "total", mode="before")
    @classmethod
    def _clamp_fields(cls, value: Any) -> int:
        return _clamp_score(value)

    @property
    def selection_score(self) -> int:
        return self.total


class TriageResult(BaseModel):
    """Strict, provider-neutral result for one raw intelligence item."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    item_id: int | str | None = None
    keep: bool = False
    topic: IntelTopic = TOPIC_OPINION
    topics: list[IntelTopic] = Field(default_factory=list)
    summary_cn: str = Field(default="")
    keywords: list[str] = Field(default_factory=list)
    selection_score: int = Field(default=0, validation_alias=AliasChoices("selection_score", "score", "display_score", "total_score"))
    scores: TriageScores = Field(default_factory=TriageScores)
    novelty: NoveltyStatus = Field(default="unknown", validation_alias=AliasChoices("novelty", "novelty_status"))
    novelty_score: int = 0
    paper_support: PaperSupport = Field(default_factory=PaperSupport)
    risk_flags: list[str] = Field(default_factory=list)
    reason: str = ""
    confidence: int = 0
    content_class: ContentClass | None = Field(
        default=None,
        validation_alias=AliasChoices("content_class", "source_content_class"),
    )
    source_group: str | None = None
    status: Literal["success", "ai_failed", "invalid", "fallback"] = "success"
    error_code: str | None = None
    error_message: str | None = None
    raw_response: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("TriageResult must be a mapping")
        data = dict(value)
        data["keep"] = _coerce_bool(data.get("keep"), False)
        if "item_id" not in data:
            for key in ("id", "raw_item_id", "normalized_item_id"):
                if key in data:
                    data["item_id"] = data[key]
                    break
        topic_value = data.get("topic")
        topics_value = data.get("topics") or data.get("topic_labels")
        if isinstance(topic_value, (list, tuple, set)):
            topics_value = topics_value or topic_value
            topic_value = next(iter(topic_value), None)
        if topic_value is None and topics_value:
            topic_value = topics_value[0] if isinstance(topics_value, (list, tuple)) else topics_value
        normalized_topic = normalize_topic(topic_value)
        if normalized_topic is None:
            raise ValueError("topic must be one of: " + ", ".join(INTEL_TOPICS))
        normalized_topics: list[IntelTopic] = [normalized_topic]
        if isinstance(topics_value, (list, tuple, set)):
            for raw_topic in topics_value:
                topic = normalize_topic(raw_topic)
                if topic is not None and topic not in normalized_topics:
                    normalized_topics.append(topic)
        data["topic"] = normalized_topic
        data["topics"] = normalized_topics
        if "summary_cn" not in data:
            data["summary_cn"] = data.get("summary") or data.get("summary_zh") or ""
        if "keywords" not in data:
            data["keywords"] = data.get("key_terms") or data.get("tags") or []
        if "selection_score" not in data:
            data["selection_score"] = data.get("score", data.get("display_score", data.get("total_score", 0)))
        scores = data.get("scores")
        if scores is None:
            score_fields = {
                key: data[key]
                for key in (
                    "relevance",
                    "relevance_score",
                    "novelty_score",
                    "impact",
                    "impact_score",
                    "heat_score",
                    "actionability",
                    "actionability_score",
                    "total",
                    "total_score",
                    "selection_score",
                )
                if key in data
            }
            data["scores"] = score_fields
        elif isinstance(scores, Mapping):
            score_fields = dict(scores)
            if not any(key in score_fields for key in ("total", "total_score", "selection_score", "score", "display_score")):
                score_fields["total"] = data.get("selection_score", 0)
            data["scores"] = score_fields
        if "novelty" not in data:
            data["novelty"] = data.get("novelty_status", "unknown")
        if "paper_support" not in data:
            data["paper_support"] = data.get("paper", data.get("paper_evidence", {}))
        if "risk_flags" not in data:
            data["risk_flags"] = data.get("risks", data.get("risk", []))
        if "content_class" not in data and data.get("source_content_class") is not None:
            data["content_class"] = data.get("source_content_class")
        if data.get("source_group") is not None:
            data["source_group"] = normalize_text(data.get("source_group")) or None
        data["summary_cn"] = normalize_text(data.get("summary_cn")) or ""
        data["reason"] = normalize_text(data.get("reason")) or ""
        data["keywords"] = _clean_list(data.get("keywords"), limit=32)
        data["risk_flags"] = _clean_list(data.get("risk_flags"), limit=32)
        data["novelty_score"] = _clamp_score(data.get("novelty_score"), 0)
        data["confidence"] = _clamp_score(data.get("confidence"), 0)
        content_class = normalize_content_class(data.get("content_class"))
        if data.get("content_class") is not None and content_class is None:
            raise ValueError("content_class is not supported")
        data["content_class"] = content_class
        for alias in (
            "id", "raw_item_id", "normalized_item_id", "summary", "summary_zh", "key_terms",
            "tags", "score", "display_score", "total_score", "novelty_status", "paper",
            "paper_evidence", "risks", "risk", "source_content_class", "topic_labels",
        ):
            data.pop(alias, None)
        return data

    @field_validator("selection_score", mode="before")
    @classmethod
    def _clamp_selection_score(cls, value: Any) -> int:
        return _clamp_score(value)

    @field_validator("novelty", mode="before")
    @classmethod
    def _normalize_novelty(cls, value: Any) -> NoveltyStatus:
        text = str(value or "unknown").strip().casefold().replace("-", "_")
        aliases = {
            "": "unknown",
            "new_item": "new",
            "novel": "new",
            "fresh": "new",
            "updated": "update",
            "version_update": "update",
            "duplicate": "repeat",
            "old": "repeat",
            "undetermined": "unknown",
        }
        text = aliases.get(text, text)
        if text not in NOVELTY_STATUSES:
            raise ValueError("novelty must be new, update, repeat, or unknown")
        return text  # type: ignore[return-value]

    @field_validator("content_class", mode="before")
    @classmethod
    def _normalize_content_class(cls, value: Any) -> ContentClass | None:
        return normalize_content_class(value) if value is not None else None

    @property
    def score(self) -> int:
        """Compatibility alias for callers that use a scalar score."""

        return self.selection_score

    @property
    def display_score(self) -> int:
        return self.selection_score

    @property
    def primary_topic(self) -> IntelTopic:
        return self.topic

    @property
    def topic_label(self) -> str:
        return INTEL_TOPIC_LABELS[self.topic]

    @property
    def novelty_status(self) -> NoveltyStatus:
        return self.novelty

    @property
    def risk(self) -> list[str]:
        return list(self.risk_flags)

    @property
    def paper_gate_pass(self) -> bool:
        return self.paper_support.hard_gate_pass if self.topic == TOPIC_PAPER else True

    def with_item(self, envelope: RawIntelEnvelope) -> "TriageResult":
        """Attach source identity without changing provider-owned fields."""

        updates: dict[str, Any] = {}
        if self.item_id is None:
            updates["item_id"] = envelope.item_id
        if self.content_class is None:
            updates["content_class"] = envelope.source_content_class
        if self.source_group is None:
            updates["source_group"] = envelope.source_group
        return self.model_copy(update=updates) if updates else self


def _is_arxiv_url(value: str) -> bool:
    try:
        host = (urlsplit(value).hostname or "").casefold()
    except ValueError:
        return False
    return host in {"arxiv.org", "export.arxiv.org"} or host.endswith(".arxiv.org")


__all__ = [
    "COMMUNITY_SOCIAL",
    "CONTENT_CLASSES",
    "CONTENT_CLASS_TO_DEFAULT_TOPIC",
    "ContentClass",
    "IntelTopic",
    "INTEL_TOPIC_LABELS",
    "INTEL_TOPICS",
    "NOVELTY_STATUSES",
    "OFFICIAL_MODEL_COMPANY",
    "PAPER_SUPPORT_LEVELS",
    "PROJECT_TOOL",
    "PaperSupport",
    "PaperSupportLevel",
    "RawIntelEnvelope",
    "SEVEN_TOPIC_TAXONOMY",
    "TOPIC_INDUSTRY",
    "TOPIC_MODEL",
    "TOPIC_OPINION",
    "TOPIC_PAPER",
    "TOPIC_PRODUCT",
    "TOPIC_PROJECT",
    "TOPIC_TUTORIAL",
    "TriageResult",
    "TriageScores",
    "normalize_content_class",
    "normalize_topic",
]
