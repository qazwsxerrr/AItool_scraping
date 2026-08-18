"""Transport-neutral DTOs for the AI-only intelligence pipeline.

The models in this module deliberately have no database, HTTP, or job
dependencies. They describe the boundaries between collectors, deterministic
selection, and structured item analysis.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping, TypeAlias
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


OFFICIAL_MODEL_COMPANY = "official_model_company"
PROJECT_TOOL = "project_tool"
COMMUNITY_SOCIAL = "community_social"
NEWS_MEDIA = "news_media"
ContentClass: TypeAlias = Literal[
    "official_model_company",
    "project_tool",
    "community_social",
    "news_media",
]
CONTENT_CLASSES: tuple[ContentClass, ...] = (
    OFFICIAL_MODEL_COMPANY,
    PROJECT_TOOL,
    COMMUNITY_SOCIAL,
    NEWS_MEDIA,
)

# Source governance vocabulary. These aliases intentionally live in the
# transport-neutral domain module so registry and storage consumers share one
# spelling without importing configuration code.
SourceTier: TypeAlias = Literal["p1", "p2", "p3", "p4"]
TopicScope: TypeAlias = Literal[
    "model_product",
    "industry_infrastructure",
    "research",
    "open_source_tool",
    "practice_opinion",
]
CanonicalSourceGroup: TypeAlias = Literal[
    "official_blog",
    "official_research",
    "tech_media",
    "github_trending",
    "github_release",
    "github_search",
    "producthunt",
    "hacker_news",
    "reddit_fixed",
    "reddit_search",
    "linux_do",
    "x_official",
    "x_social",
    "x_search",
]
CANONICAL_SOURCE_GROUPS: tuple[CanonicalSourceGroup, ...] = (
    "official_blog",
    "official_research",
    "tech_media",
    "github_trending",
    "github_release",
    "github_search",
    "producthunt",
    "hacker_news",
    "reddit_fixed",
    "reddit_search",
    "linux_do",
    "x_official",
    "x_social",
    "x_search",
)


class SelectionPolicy(BaseModel):
    """Typed common rules with room for collector-specific policy fields."""

    model_config = ConfigDict(extra="allow", frozen=True)

    mode: str = "default"
    max_age_days: int | None = None
    time_window_days: int | None = None
    pushed_days: int | None = None
    min_stars: int | None = None
    min_votes: int | None = None
    min_engagement: float | None = None
    keywords: tuple[str, ...] = ()
    sort_by: str | None = None
    sort_order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="before")
    @classmethod
    def _normalise_input(cls, value: Any) -> Any:
        if value is None or isinstance(value, cls):
            return value or {}
        if not isinstance(value, Mapping):
            raise TypeError("selection_policy must be a mapping or SelectionPolicy")

        data = dict(value)
        if "window_days" in data and "max_age_days" not in data:
            data["max_age_days"] = data["window_days"]
        if "stars" in data and "min_stars" not in data and isinstance(data["stars"], (int, float)):
            data["min_stars"] = data["stars"]
        if "votes" in data and "min_votes" not in data and isinstance(data["votes"], (int, float)):
            data["min_votes"] = data["votes"]
        if "engagement" in data and "min_engagement" not in data and isinstance(data["engagement"], (int, float)):
            data["min_engagement"] = data["engagement"]
        if "sort" in data and "sort_by" not in data:
            data["sort_by"] = data["sort"]
        if "keyword" in data and "keywords" not in data:
            data["keywords"] = data["keyword"]
        if isinstance(data.get("keywords"), str):
            data["keywords"] = tuple(
                part.strip()
                for part in data["keywords"].replace("，", ",").split(",")
                if part.strip()
            )
        return data

    @model_validator(mode="after")
    def _validate_ranges(self) -> "SelectionPolicy":
        for name in ("max_age_days", "time_window_days", "pushed_days"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when configured")
        for name in ("min_stars", "min_votes", "min_engagement"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative when configured")
        return self


Transport: TypeAlias = Literal["feed", "rsshub", "github"]
SourceTransport: TypeAlias = Transport
FeedFormat: TypeAlias = Literal["rss", "atom"]
FeedAdapter: TypeAlias = Literal["generic", "producthunt"]
GitHubMode: TypeAlias = Literal["search", "releases", "trending"]
GitHubSort: TypeAlias = Literal["stars", "forks", "help-wanted-issues", "updated"]
TrendingPeriod: TypeAlias = Literal["daily", "weekly"]


class FeedOptions(BaseModel):
    """Options for native and RSSHub feed transports.

    RSSHub routes produce a regular RSS/Atom document.  Keeping the parser
    format and the Product Hunt adapter in this small nested model makes feed
    routing explicit without reintroducing collector/parser discriminator
    fields on the source itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: FeedFormat = "rss"
    adapter: FeedAdapter = "generic"

    @model_validator(mode="after")
    def _validate_adapter(self) -> "FeedOptions":
        if self.adapter == "producthunt" and self.format != "atom":
            raise ValueError("producthunt feed adapter requires feed.format=atom")
        return self


class GitHubOptions(BaseModel):
    """Mode-specific options for GitHub Search, Releases, and Trending."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    mode: GitHubMode
    query: str | None = Field(default=None, validation_alias=AliasChoices("query", "search_query"))
    sort: GitHubSort = Field(default="updated", validation_alias=AliasChoices("sort", "search_sort"))
    order: Literal["asc", "desc"] = Field(default="desc", validation_alias=AliasChoices("order", "search_order"))
    pushed_days: int | None = Field(
        default=None,
        validation_alias=AliasChoices("pushed_days", "search_pushed_days"),
    )
    period: TrendingPeriod | None = None

    @model_validator(mode="after")
    def _validate_mode_options(self) -> "GitHubOptions":
        if self.mode == "search":
            if not self.query or not self.query.strip():
                raise ValueError("github search mode requires github.query")
            if self.pushed_days is not None and self.pushed_days <= 0:
                raise ValueError("github.pushed_days must be positive when configured")
        elif self.mode == "releases":
            if self.query is not None or self.pushed_days is not None or self.period is not None:
                raise ValueError("github releases mode does not accept search/trending options")
        elif self.mode == "trending":
            if self.query is not None or self.pushed_days is not None:
                raise ValueError("github trending mode does not accept search options")
            if self.period is None:
                raise ValueError("github trending mode requires github.period")
        return self

    @property
    def search_query(self) -> str | None:
        """Return the configured GitHub search query."""

        return self.query

    @property
    def search_sort(self) -> GitHubSort:
        return self.sort

    @property
    def search_order(self) -> Literal["asc", "desc"]:
        return self.order

    @property
    def search_pushed_days(self) -> int | None:
        return self.pushed_days


class SourceSpec(BaseModel):
    """Canonical registry source with resolved content and policy metadata.

    ``transport`` is the only top-level routing discriminator.  Feed details
    live under :attr:`feed`; GitHub mode details live under :attr:`github`.
    Unsupported ``type``, ``parser_type`` and ``collector_type`` keys are
    rejected instead of being silently inferred.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str = Field(min_length=1)
    name: str | None = None
    transport: Transport
    url: str = Field(min_length=1)
    feed: FeedOptions | None = None
    github: GitHubOptions | None = None
    enabled: bool = True
    priority: int = 100
    fetch_interval: int = 3600
    source_group: str | None = None
    source_subtype: str | None = None
    tier: SourceTier = "p4"
    topic_scopes: tuple[TopicScope, ...] = ()
    primary_eligible: bool = False
    quality_weight: float | None = None
    source_role: str | None = None
    spam_risk: Literal["low", "medium", "high"] | None = None
    account_url: str | None = None
    bypass_proxy: bool = False
    default_limit: int = 30
    content_class: ContentClass | None = None
    selection_policy: SelectionPolicy = Field(default_factory=SelectionPolicy)

    @model_validator(mode="after")
    def _validate_transport(self) -> "SourceSpec":
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL")
        if self.priority < 0:
            raise ValueError("priority must be non-negative")
        if self.fetch_interval <= 0:
            raise ValueError("fetch_interval must be positive")
        if self.default_limit <= 0:
            raise ValueError("default_limit must be positive")
        if self.quality_weight is not None and not 0 <= self.quality_weight <= 1:
            raise ValueError("quality_weight must be between 0 and 1")
        if self.source_group is not None and not _valid_source_token(self.source_group):
            raise ValueError("source_group must contain lowercase letters, numbers, underscore or dash")
        if self.source_subtype is not None and not _valid_source_token(self.source_subtype):
            raise ValueError("source_subtype must contain lowercase letters, numbers, underscore or dash")
        if self.transport in {"feed", "rsshub"}:
            if self.github is not None:
                raise ValueError(f"{self.transport} source cannot define github options")
            if self.feed is None:
                object.__setattr__(self, "feed", FeedOptions())
            elif self.transport == "rsshub" and self.feed.adapter == "producthunt":
                raise ValueError("producthunt feed adapter is only valid for transport=feed")
        elif self.transport == "github":
            if self.feed is not None:
                raise ValueError("github source cannot define feed options")
            if self.github is None:
                raise ValueError("github source requires github options")
        return self

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _valid_source_token(value):
            raise ValueError("id must contain lowercase letters, numbers, underscore or dash")
        return value

    @classmethod
    def from_config(cls, source: Any) -> "SourceSpec":
        # Lazy import avoids a models -> policies -> models cycle.
        from .policies import source_spec_from_config

        return source_spec_from_config(source)


def _valid_source_token(value: str) -> bool:
    import re

    return re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", value) is not None


# Descriptive aliases make the nested model easy to discover while keeping a
# single implementation and a single canonical serialized shape.
FeedConfig = FeedOptions
GitHubConfig = GitHubOptions


class FetchItem(BaseModel):
    """Canonical item exchanged between collectors and AI review."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    item_id: int | str | None = Field(default=None, alias="id")
    source_id: str = Field(min_length=1)
    content_class: ContentClass | None = None
    external_id: str | None = None
    title: str = Field(min_length=1)
    url: str | None = None
    canonical_url: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    captured_at: datetime | None = None
    summary: str | None = None
    content: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    kind: str = "unknown"

    @model_validator(mode="before")
    @classmethod
    def _normalise_collector_fields(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            # Accept ParsedFeedItem and similar lightweight objects without
            # importing their module into this pure domain layer.
            keys = (
                "id",
                "item_id",
                "source_id",
                "external_id",
                "title",
                "url",
                "link",
                "canonical_url",
                "author",
                "published_at",
                "captured_at",
                "summary",
                "raw_summary",
                "content",
                "raw_content",
                "metrics",
                "raw_payload",
                "kind",
            )
            value = {key: getattr(value, key) for key in keys if hasattr(value, key)}

        data = dict(value)
        if "item_id" not in data and "id" in data:
            data["item_id"] = data["id"]
        if not data.get("url") and data.get("link"):
            data["url"] = data["link"]
        if not data.get("canonical_url") and data.get("url"):
            data["canonical_url"] = data["url"]
        if "summary" not in data:
            data["summary"] = data.get("raw_summary")
        if "content" not in data:
            data["content"] = data.get("raw_content")

        payload_value = data.get("raw_payload")
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
        data["raw_payload"] = payload
        metrics_value = data.get("metrics")
        metrics = dict(metrics_value) if isinstance(metrics_value, Mapping) else {}

        # Existing GitHub and Product Hunt adapters place metrics in payloads.
        for target, aliases in {
            "stars": ("stars", "stargazers_count", "star_count"),
            "forks": ("forks", "forks_count", "fork_count"),
            "votes": ("votes", "vote_count", "upvotes", "points"),
            "comments": ("comments", "comments_count", "comment_count"),
            "likes": ("likes", "like_count", "favorite_count"),
            "reposts": ("reposts", "retweets", "retweet_count", "shares"),
            "replies": ("replies", "reply_count", "quote_count"),
            "views": ("views", "view_count", "impressions"),
            "engagement": ("engagement", "engagement_count", "score"),
            "pushed_at": ("pushed_at", "updated_at"),
        }.items():
            if target in metrics:
                continue
            for alias in aliases:
                if alias in data and data[alias] is not None:
                    metrics[target] = data[alias]
                    break
                if alias in payload and payload[alias] is not None:
                    metrics[target] = payload[alias]
                    break
        data["metrics"] = metrics
        return data

    @property
    def id(self) -> int | str | None:
        return self.item_id

    @property
    def link(self) -> str | None:
        return self.url

    @property
    def body_text(self) -> str | None:
        return self.content or self.summary


class FetchBatch(BaseModel):
    """Collector output plus transport telemetry, before persistence."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    source_id: str | None = None
    source: SourceSpec | None = None
    items: list[FetchItem] = Field(default_factory=list)
    captured_at: datetime | None = None
    status: str = "success"
    http_status: int | None = None
    request_url: str | None = None
    final_url: str | None = None
    # Conditional feed request metadata.  These fields are optional so older
    # collectors and persisted batches remain fully backwards compatible.
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    response_bytes: int = 0
    retry_count: int = 0
    transport: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _resolve_source(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        source = data.get("source")
        if source is not None and not data.get("source_id"):
            if isinstance(source, str):
                data["source_id"] = source
                data.pop("source", None)
            elif isinstance(source, Mapping):
                data["source_id"] = source.get("id")
            elif hasattr(source, "id"):
                data["source_id"] = getattr(source, "id")
        return data

    @model_validator(mode="after")
    def _validate_source(self) -> "FetchBatch":
        if not self.source_id and self.source is None:
            raise ValueError("FetchBatch requires source_id or source")
        if self.source_id is None and self.source is not None:
            self.source_id = self.source.id
        if self.response_bytes < 0:
            raise ValueError("response_bytes must be non-negative")
        if self.retry_count < 0:
            raise ValueError("retry_count must be non-negative")
        return self

    @property
    def items_fetched(self) -> int:
        return len(self.items)

    @property
    def error(self) -> str | None:
        return self.error_message

    @property
    def status_code(self) -> int | None:
        return self.http_status


class SelectionDecision(BaseModel):
    """Explainable result of deterministic source-specific selection."""

    model_config = ConfigDict(frozen=True, extra="allow")

    selected: bool
    reason: str
    content_class: ContentClass
    mode: str
    score: float = 0.0
    matched_keywords: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()

    @property
    def keep(self) -> bool:
        return self.selected
