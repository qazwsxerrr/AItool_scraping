from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GitHubProjectProfile:
    normalized_item_id: int
    raw_item_id: int
    source_id: str
    title: str
    url: str | None
    published_at: str | None
    repo_full_name: str
    owner: str | None = None
    repo: str | None = None
    description: str | None = None
    homepage: str | None = None
    topics: list[str] = field(default_factory=list)
    primary_language: str | None = None
    languages: dict[str, int] = field(default_factory=dict)
    stars: int = 0
    forks: int = 0
    watchers: int = 0
    open_issues: int = 0
    license_name: str | None = None
    archived: bool = False
    fork: bool = False
    created_at: str | None = None
    updated_at: str | None = None
    pushed_at: str | None = None
    readme_excerpt: str | None = None
    readme_text: str | None = None
    latest_releases: list[dict[str, Any]] = field(default_factory=list)
    profile_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GitHubProjectDigest:
    project_name: str
    summary_cn: str
    description_cn: str
    keywords: list[str]
    project_type: str
    target_users: list[str]
    main_features: list[str]
    how_to_try: str | None
    risk_notes: list[str]
    is_ai_related: bool
    ai_relevance_score: int
    readme_quality_score: int
    usability_score: int
    digest_confidence: int
    digest_source: str = "rules_fallback"
    raw_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class GitHubProjectRanking:
    ai_relevance_score: int
    popularity_score: int
    activity_score: int
    quality_score: int
    usability_score: int
    risk_score: int
    final_score: int
    level: str
    decision: str
    rank_reason: str
    risk_flags: list[str]


@dataclass(frozen=True)
class GitHubProjectReportItem:
    profile: GitHubProjectProfile
    digest: GitHubProjectDigest
    ranking: GitHubProjectRanking
