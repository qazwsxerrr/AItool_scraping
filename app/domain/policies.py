"""Deterministic, source-specific content selection policies."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pydantic import BaseModel

from .models import (
    COMMUNITY_SOCIAL,
    CONTENT_CLASSES,
    OFFICIAL_MODEL_COMPANY,
    PROJECT_TOOL,
    ContentClass,
    FetchItem,
    SelectionDecision,
    SelectionPolicy,
    SourceSpec,
    VerificationPolicy,
)


DEFAULT_OFFICIAL_KEYWORDS: tuple[str, ...] = (
    "model",
    "company",
    "release",
    "released",
    "update",
    "updated",
    "upgrade",
    "launch",
    "announcing",
    "api",
    "version",
    "price",
    "pricing",
    "weights",
    "checkpoint",
    "模型",
    "发布",
    "上线",
    "版本",
    "权重",
    "价格",
)
DEFAULT_COMMUNITY_KEYWORDS: tuple[str, ...] = (
    "ai",
    "model",
    "agent",
    "mcp",
    "rag",
    "github",
    "tool",
    "release",
    "模型",
    "项目",
    "工具",
)

_OFFICIAL_ROLES = {"official"}
_PROJECT_ROLES = {"code_hosting", "launch_platform"}
_COMMUNITY_ROLES = {"community", "social", "social_search", "forum", "search"}


def classify_source(source: Any) -> ContentClass:
    """Classify a registry source, preferring an explicit ``content_class``.

    ``source`` may be a current ``SourceConfig``, a future registry model, a
    ``SourceSpec``, a mapping, or a lightweight object used in tests.
    """

    raw = _source_mapping(source)
    explicit = _normalise_content_class(raw.get("content_class"))
    if explicit is not None:
        return explicit

    source_group = _normalise_token(raw.get("source_group"))
    source_url = str(raw.get("url") or "").casefold()
    # Social transports remain discovery signals even when the account itself
    # is an official company account. A registry entry may explicitly override
    # this only by setting content_class.
    if (
        source_group == "x"
        or source_group.startswith("reddit")
        or source_group == "linux_do"
        or "/twitter/" in source_url
        or "/reddit/" in source_url
    ):
        return COMMUNITY_SOCIAL

    role = _normalise_token(raw.get("source_role"))
    transport = _normalise_token(raw.get("transport"))
    group = _normalise_token(raw.get("source_group"))
    subtype = _normalise_token(raw.get("source_subtype"))
    source_id = _normalise_token(raw.get("id"))
    # X/RSSHub account and search feeds remain discovery sources even when the
    # account itself is official. The official article they link to is the
    # direct source link, not the social post.
    if transport == "rsshub" and (
        group == "x"
        or source_id.startswith("x_")
        or role in {"social", "social_search"}
        or subtype in {"account", "search"}
    ):
        return COMMUNITY_SOCIAL
    if role in _OFFICIAL_ROLES:
        return OFFICIAL_MODEL_COMPANY
    if role in _PROJECT_ROLES:
        return PROJECT_TOOL
    if role in _COMMUNITY_ROLES:
        return COMMUNITY_SOCIAL

    if transport == "github":
        return PROJECT_TOOL

    identity = " ".join(
        str(raw.get(key) or "")
        for key in ("id", "name", "source_group", "source_subtype", "url")
    ).casefold()
    if any(token in identity for token in ("github", "producthunt", "product hunt", "code_hosting")):
        return PROJECT_TOOL
    if any(
        token in identity
        for token in (
            "official",
            "openai",
            "deepmind",
            "anthropic",
            "mistral",
            "huggingface blog",
            "hugging face blog",
        )
    ):
        return OFFICIAL_MODEL_COMPANY
    if any(
        token in identity
        for token in ("reddit", "twitter", "x.com", "linux_do", "linux do", "forum", "community", "social")
    ):
        return COMMUNITY_SOCIAL
    return COMMUNITY_SOCIAL


def source_spec_from_config(source: Any) -> SourceSpec:
    """Resolve registry metadata into a complete, immutable ``SourceSpec``."""

    # A resolved spec can be passed between pipeline stages repeatedly. Keep
    # its already-resolved policy instead of feeding Pydantic's default values
    # back through the inference/merge step.
    if isinstance(source, SourceSpec) and source.content_class is not None:
        selection_is_resolved = (
            source.selection_policy.mode != "default"
            or "mode" in source.selection_policy.model_fields_set
        )
        verification_is_resolved = (
            source.verification_policy.mode != "metadata_only"
            or "mode" in source.verification_policy.model_fields_set
        )
        if selection_is_resolved and verification_is_resolved:
            return source

    raw = _source_mapping(source)
    source_id = str(raw.get("id") or "").strip()
    if not source_id:
        raise ValueError("source id is required")

    # Validate the canonical transport/options before resolving policies.  In
    # particular, this rejects the removed ``type``, ``parser_type`` and
    # ``collector_type`` keys instead of silently guessing a route.
    canonical = SourceSpec.model_validate(
        {
            **raw,
            "id": source_id,
            "name": raw.get("name") or source_id,
        }
    )
    content_class = classify_source(canonical)
    selection_defaults = _default_selection_policy(raw, content_class)
    selection_explicit = _policy_mapping(raw.get("selection_policy"))
    selection = SelectionPolicy.model_validate({**selection_defaults, **selection_explicit})

    verification_defaults = _default_verification_policy(content_class)
    verification_explicit = _policy_mapping(raw.get("verification_policy"))
    verification = VerificationPolicy.model_validate(
        {**verification_defaults, **verification_explicit}
    )

    data = canonical.model_dump(exclude={"selection_policy", "verification_policy"})
    data.update(
        {
            "id": source_id,
            "name": canonical.name or source_id,
            "content_class": content_class,
            "selection_policy": selection,
            "verification_policy": verification,
        }
    )
    return SourceSpec.model_validate(data)


def selection_decision(
    item: FetchItem | Mapping[str, Any] | Any,
    source: SourceSpec | Mapping[str, Any] | Any,
    *,
    now: datetime | None = None,
) -> SelectionDecision:
    """Evaluate one item and return a reasoned deterministic decision."""

    fetch_item = _coerce_item(item)
    spec = source_spec_from_config(source)
    current_time = _normalise_datetime(now or datetime.now(timezone.utc))
    policy = spec.selection_policy
    mode = policy.mode.casefold().replace("-", "_")
    metadata_only_project = (
        spec.content_class == PROJECT_TOOL
        and (
            spec.transport == "github"
            or mode in {"github_active_high_star", "active_high_star", "github_trending"}
        )
    )
    # GitHub repository inclusion is a metadata rule, not a composite score.
    score = 0.0 if metadata_only_project else _score(fetch_item, spec, current_time)

    common = {
        "content_class": spec.content_class,
        "mode": policy.mode,
        "score": score,
        "verification_mode": spec.verification_policy.mode,
        "discovery_only": policy.discovery_only or spec.verification_policy.discovery_only,
    }
    if not spec.enabled:
        return SelectionDecision(selected=False, reason="source_disabled", **common)

    if spec.content_class == OFFICIAL_MODEL_COMPANY:
        return _select_official(fetch_item, policy, current_time, common)
    if spec.content_class == PROJECT_TOOL:
        return _select_project(fetch_item, spec, policy, current_time, common)
    return _select_community(fetch_item, policy, current_time, common)


def should_select(
    item: FetchItem | Mapping[str, Any] | Any,
    source: SourceSpec | Mapping[str, Any] | Any,
    *,
    now: datetime | None = None,
) -> bool:
    """Boolean convenience wrapper around :func:`selection_decision`."""

    return selection_decision(item, source, now=now).selected


def _select_official(
    item: FetchItem,
    policy: SelectionPolicy,
    now: datetime,
    common: dict[str, Any],
) -> SelectionDecision:
    window = policy.max_age_days or policy.time_window_days or 30
    published = _item_datetime(item, "published_at")
    if published is None:
        return SelectionDecision(
            selected=False,
            reason="official_missing_published_at",
            risk_flags=("missing_published_at",),
            **common,
        )
    if not _within_days(published, now, window):
        return SelectionDecision(selected=False, reason="official_item_too_old", **common)

    keywords = policy.keywords or DEFAULT_OFFICIAL_KEYWORDS
    matched = _matched_keywords(item, keywords)
    if keywords and not matched:
        return SelectionDecision(selected=False, reason="official_keyword_missing", **common)
    return SelectionDecision(
        selected=True,
        reason="selected:official_recent_keyword",
        matched_keywords=matched,
        **common,
    )


def _select_project(
    item: FetchItem,
    source: SourceSpec,
    policy: SelectionPolicy,
    now: datetime,
    common: dict[str, Any],
) -> SelectionDecision:
    mode = policy.mode.casefold().replace("-", "_")
    if _is_github_trending(source) or mode == "github_trending":
        stars_since = _metric_number(item, "stars_since", "trending_stars", "stars_today", "stars_this_week")
        if stars_since <= 0:
            return SelectionDecision(
                selected=False,
                reason="github_trending_missing_period_stars",
                risk_flags=("missing_stars_since",),
                **common,
            )
        if _truthy_metric(item, "archived") or _truthy_metric(item, "fork"):
            return SelectionDecision(selected=False, reason="github_trending_archived_or_fork", **common)
        return SelectionDecision(selected=True, reason="selected:github_trending", **common)

    if _is_github_search(source) or mode in {"github_active_high_star", "active_high_star"}:
        stars = _metric_number(item, "stars", "stargazers_count", "star_count")
        minimum = float(policy.min_stars if policy.min_stars is not None else 100)
        if stars <= minimum:
            return SelectionDecision(
                selected=False,
                reason="github_stars_below_threshold",
                risk_flags=(("missing_stars",) if stars == 0 else ()),
                **common,
            )
        pushed = _item_datetime(item, "pushed_at", "updated_at")
        if pushed is None:
            return SelectionDecision(
                selected=False,
                reason="github_missing_pushed_at",
                risk_flags=("missing_pushed_at",),
                **common,
            )
        pushed_days = policy.pushed_days or 30
        if not _within_days(pushed, now, pushed_days):
            return SelectionDecision(selected=False, reason="github_push_too_old", **common)
        return SelectionDecision(selected=True, reason="selected:github_active_high_star", **common)

    if _is_producthunt(source) or mode in {"producthunt_hot", "product_hunt_hot"}:
        published = _item_datetime(item, "published_at")
        window = policy.max_age_days or policy.time_window_days or 30
        if published is None:
            return SelectionDecision(
                selected=False,
                reason="producthunt_missing_published_at",
                risk_flags=("missing_published_at",),
                **common,
            )
        if not _within_days(published, now, window):
            return SelectionDecision(selected=False, reason="producthunt_item_too_old", **common)
        votes = _metric_number(item, "votes", "vote_count", "upvotes", "points")
        minimum = float(policy.min_votes or 0)
        github_linked = bool(
            item.metrics.get("github_url")
            or item.metrics.get("canonical_project_key")
            or item.metrics.get("github_metadata_fetched")
        )
        if votes < minimum and not github_linked:
            return SelectionDecision(selected=False, reason="producthunt_votes_below_threshold", **common)
        flags = ("missing_votes",) if votes == 0 else ()
        return SelectionDecision(
            selected=True,
            reason=("selected:producthunt_github_metadata" if github_linked else "selected:producthunt_votes_and_recency"),
            risk_flags=flags,
            **common,
        )

    published = _item_datetime(item, "published_at")
    window = policy.max_age_days or policy.time_window_days or 30
    if published is not None and not _within_days(published, now, window):
        return SelectionDecision(selected=False, reason="project_item_too_old", **common)
    return SelectionDecision(selected=True, reason="selected:project_tool", **common)


def _select_community(
    item: FetchItem,
    policy: SelectionPolicy,
    now: datetime,
    common: dict[str, Any],
) -> SelectionDecision:
    published = _item_datetime(item, "published_at")
    window = policy.max_age_days or policy.time_window_days or 7
    if published is None:
        return SelectionDecision(
            selected=False,
            reason="community_missing_published_at",
            risk_flags=("missing_published_at",),
            **common,
        )
    if not _within_days(published, now, window):
        return SelectionDecision(selected=False, reason="community_item_too_old", **common)
    keywords = policy.keywords
    if not keywords:
        keywords = DEFAULT_COMMUNITY_KEYWORDS
    matched = _matched_keywords(item, keywords)
    engagement = _metric_number(
        item,
        "engagement",
        "likes",
        "reposts",
        "retweets",
        "replies",
        "comments",
        "comments_count",
        "num_comments",
        "votes",
        "upvotes",
        "score",
        "views",
        "view_count",
        "likes",
        "reposts",
        "retweets",
        "replies",
        "quote_count",
    )
    min_engagement = float(policy.min_engagement) if policy.min_engagement is not None else 1.0
    if keywords and not matched and engagement < min_engagement:
        return SelectionDecision(selected=False, reason="community_keyword_missing", **common)
    reason = "selected:community_keyword" if matched else "selected:community_engagement"
    return SelectionDecision(
        selected=True,
        reason=reason,
        matched_keywords=matched,
        **common,
    )


def _default_selection_policy(raw: Mapping[str, Any], content_class: ContentClass) -> dict[str, Any]:
    if content_class == OFFICIAL_MODEL_COMPANY:
        return {
            "mode": "official_recent",
            "max_age_days": 30,
            "keywords": DEFAULT_OFFICIAL_KEYWORDS,
            "sort_by": "published_at",
            "sort_order": "desc",
        }
    if content_class == COMMUNITY_SOCIAL:
        return {
            "mode": "community_recent",
            "max_age_days": 7,
            "sort_by": "published_at",
            "sort_order": "desc",
            "discovery_only": True,
        }
    if _is_github_trending(raw):
        return {
            "mode": "github_trending",
            "sort_by": "stars_since",
            "sort_order": "desc",
        }
    if _is_github_search(raw):
        return {
            "mode": "github_active_high_star",
            "pushed_days": _github_pushed_days(raw) or 30,
            "min_stars": 100,
            "sort_by": "stars",
            "sort_order": "desc",
        }
    if _is_producthunt(raw):
        return {
            "mode": "producthunt_hot",
            "max_age_days": 30,
            "min_votes": 0,
            "sort_by": "votes",
            "sort_order": "desc",
        }
    return {
        "mode": "project_recent",
        "max_age_days": 30,
        "sort_by": "published_at",
        "sort_order": "desc",
    }


def _default_verification_policy(content_class: ContentClass) -> dict[str, Any]:
    if content_class == OFFICIAL_MODEL_COMPANY:
        return {
            "mode": "official_direct_link",
            "required": True,
            "direct_link": True,
            "discovery_only": False,
        }
    if content_class == COMMUNITY_SOCIAL:
        return {
            "mode": "discovery_only",
            "required": False,
            "direct_link": False,
            "discovery_only": True,
        }
    return {
        "mode": "metadata_only",
        "required": False,
        "direct_link": False,
        "discovery_only": False,
    }


def _source_mapping(source: Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    if isinstance(source, SourceSpec):
        # ``model_dump()`` materializes defaults and would make them look like
        # explicit registry overrides. Preserve only fields the caller set on
        # an unresolved spec; resolved specs are returned early above.
        data = source.model_dump(exclude={"selection_policy", "verification_policy"})
        if source.selection_policy.model_fields_set:
            data["selection_policy"] = source.selection_policy.model_dump(exclude_unset=True)
        if source.verification_policy.model_fields_set:
            data["verification_policy"] = source.verification_policy.model_dump(exclude_unset=True)
        if source.model_extra:
            data.update(source.model_extra)
        return data
    if isinstance(source, BaseModel):
        data = source.model_dump()
        if source.model_extra:
            data.update(source.model_extra)
        return data
    keys = (
        "id",
        "name",
        "transport",
        "url",
        "enabled",
        "priority",
        "fetch_interval",
        "feed",
        "github",
        "source_group",
        "source_subtype",
        "quality_weight",
        "source_role",
        "spam_risk",
        "requires_verification",
        "default_limit",
        "content_class",
        "selection_policy",
        "verification_policy",
    )
    return {key: getattr(source, key) for key in keys if hasattr(source, key)}


def _policy_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        data = value.model_dump(exclude_unset=True)
        if value.model_extra:
            data.update(value.model_extra)
        return data
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("source policy must be a mapping or Pydantic model")


def _coerce_item(item: Any) -> FetchItem:
    if isinstance(item, FetchItem):
        return item
    return FetchItem.model_validate(item, from_attributes=True)


def _is_github_search(source: SourceSpec | Mapping[str, Any] | Any) -> bool:
    raw = _source_mapping(source)
    transport = _normalise_token(raw.get("transport"))
    github = _nested_mapping(raw.get("github"))
    github_mode = _normalise_token(github.get("mode"))
    mode = _normalise_token(_policy_mapping(raw.get("selection_policy")).get("mode"))
    if mode in {"github_active_high_star", "active_high_star"}:
        return True
    return transport == "github" and github_mode == "search"


def _is_github_trending(source: SourceSpec | Mapping[str, Any] | Any) -> bool:
    raw = _source_mapping(source)
    transport = _normalise_token(raw.get("transport"))
    github = _nested_mapping(raw.get("github"))
    github_mode = _normalise_token(github.get("mode"))
    mode = _normalise_token(_policy_mapping(raw.get("selection_policy")).get("mode"))
    return bool(
        transport == "github" and github_mode == "trending"
        or mode == "github_trending"
    )


def _is_producthunt(source: SourceSpec | Mapping[str, Any] | Any) -> bool:
    raw = _source_mapping(source)
    feed = _nested_mapping(raw.get("feed"))
    if _normalise_token(raw.get("transport")) == "feed" and _normalise_token(feed.get("adapter")) == "producthunt":
        return True
    identity = " ".join(
        str(raw.get(key) or "") for key in ("id", "name", "source_group", "url")
    ).casefold()
    return "producthunt" in identity or "product hunt" in identity


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump()
    return {}


def _github_pushed_days(raw: Mapping[str, Any]) -> int | None:
    github = _nested_mapping(raw.get("github"))
    value = github.get("pushed_days")
    return _positive_int(value)


def _matched_keywords(item: FetchItem, keywords: tuple[str, ...]) -> tuple[str, ...]:
    haystack = "\n".join(
        part for part in (item.title, item.summary, item.content) if part
    ).casefold()
    matched: list[str] = []
    for keyword in keywords:
        cleaned = str(keyword).strip()
        if cleaned and _keyword_matches_text(cleaned, haystack) and cleaned not in matched:
            matched.append(cleaned)
    return tuple(matched)


def _keyword_matches_text(keyword: str, haystack: str) -> bool:
    """Avoid two-letter ASCII keywords (notably ``ai``) matching substrings."""

    needle = keyword.casefold()
    if not needle:
        return False
    if needle.isascii() and needle.isalnum() and len(needle) <= 2:
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


def _metric_number(item: FetchItem, *names: str) -> float:
    for container in (item.metrics, item.raw_payload):
        for name in names:
            value = container.get(name)
            parsed = _parse_number(value)
            if parsed is not None:
                return parsed
    return 0.0


def _truthy_metric(item: FetchItem, name: str) -> bool:
    for container in (item.metrics, item.raw_payload):
        value = container.get(name)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
        if value is not None:
            return bool(value)
    return False


def _item_datetime(item: FetchItem, *names: str) -> datetime | None:
    for name in names:
        if name == "published_at" and item.published_at is not None:
            return _normalise_datetime(item.published_at)
        if name == "captured_at" and item.captured_at is not None:
            return _normalise_datetime(item.captured_at)
        for container in (item.metrics, item.raw_payload):
            parsed = _parse_datetime(container.get(name))
            if parsed is not None:
                return parsed
    return None


def _within_days(value: datetime, now: datetime, days: int) -> bool:
    lower_bound = now - timedelta(days=days)
    # Tolerate modest future timestamps caused by source clock skew.
    return lower_bound <= value <= now + timedelta(hours=24)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _normalise_datetime(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return _normalise_datetime(datetime.fromisoformat(text))
    except ValueError:
        return None


def _normalise_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace(",", "")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([km]?)", text)
    if not match:
        return None
    amount = float(match.group(1))
    multiplier = {"": 1.0, "k": 1_000.0, "m": 1_000_000.0}[match.group(2)]
    return amount * multiplier


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalise_token(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def _normalise_content_class(value: Any) -> ContentClass | None:
    normalised = _normalise_token(value)
    if normalised in CONTENT_CLASSES:
        return normalised  # type: ignore[return-value]
    return None


def _score(item: FetchItem, source: SourceSpec, now: datetime) -> float:
    # Local import prevents a policies/scoring import cycle.
    from .scoring import score_item

    return score_item(item, source, now=now)


# Private helpers are imported by scoring.py to keep metric/date semantics
# consistent across selection and ranking.
metric_number = _metric_number
item_datetime = _item_datetime
within_days = _within_days
normalise_datetime = _normalise_datetime
matched_keywords = _matched_keywords
