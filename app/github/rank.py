from __future__ import annotations

import math
from datetime import datetime, timezone

from dateutil import parser as date_parser

from app.github.project_types import GitHubProjectDigest, GitHubProjectProfile, GitHubProjectRanking


def rank_project(profile: GitHubProjectProfile, digest: GitHubProjectDigest) -> GitHubProjectRanking:
    popularity = _popularity_score(profile)
    activity = _activity_score(profile)
    quality = _quality_score(profile, digest)
    usability = digest.usability_score
    risk_score, risk_flags = _risk_score(profile, digest)
    ai_relevance = digest.ai_relevance_score

    final_score = round(
        ai_relevance * 0.30
        + popularity * 0.20
        + activity * 0.20
        + quality * 0.15
        + usability * 0.10
        - risk_score * 0.05
    )
    final_score = _clamp(final_score)
    level, decision = _level_and_decision(final_score, ai_relevance, risk_score, profile)
    return GitHubProjectRanking(
        ai_relevance_score=ai_relevance,
        popularity_score=popularity,
        activity_score=activity,
        quality_score=quality,
        usability_score=usability,
        risk_score=risk_score,
        final_score=final_score,
        level=level,
        decision=decision,
        rank_reason=_rank_reason(profile, digest, popularity, activity, quality, risk_score),
        risk_flags=risk_flags,
    )


def _popularity_score(profile: GitHubProjectProfile) -> int:
    stars = _log_score(profile.stars, max_value=10000)
    forks = _log_score(profile.forks, max_value=3000)
    watchers = _log_score(profile.watchers, max_value=10000)
    return _clamp(round(stars * 0.55 + forks * 0.30 + watchers * 0.15))


def _activity_score(profile: GitHubProjectProfile) -> int:
    days = _days_since(profile.pushed_at or profile.updated_at)
    if days is None:
        return 25
    if days <= 7:
        score = 95
    elif days <= 30:
        score = 80
    elif days <= 90:
        score = 60
    elif days <= 180:
        score = 40
    else:
        score = 20
    if profile.latest_releases:
        score += 8
    return _clamp(score)


def _quality_score(profile: GitHubProjectProfile, digest: GitHubProjectDigest) -> int:
    score = round(digest.readme_quality_score * 0.60)
    if profile.license_name:
        score += 12
    if profile.topics:
        score += 8
    if profile.primary_language:
        score += 6
    if profile.latest_releases:
        score += 8
    if profile.homepage:
        score += 6
    return _clamp(score)


def _risk_score(profile: GitHubProjectProfile, digest: GitHubProjectDigest) -> tuple[int, list[str]]:
    score = 0
    flags: list[str] = []
    if profile.archived:
        score += 80
        flags.append("archived")
    if profile.fork:
        score += 20
        flags.append("fork")
    if not profile.readme_excerpt:
        score += 25
        flags.append("missing_readme")
    if not profile.license_name:
        score += 12
        flags.append("missing_license")
    if profile.stars < 10:
        score += 15
        flags.append("low_stars")
    if profile.profile_errors:
        score += min(25, len(profile.profile_errors) * 8)
        flags.append("partial_github_enrich_failure")
    if digest.risk_notes:
        score += min(20, len(digest.risk_notes) * 5)
    return _clamp(score), flags


def _level_and_decision(score: int, ai_relevance: int, risk_score: int, profile: GitHubProjectProfile) -> tuple[str, str]:
    if profile.archived or risk_score >= 80:
        return "D", "仅归档"
    if ai_relevance < 40:
        return "D", "排除：AI 相关度低"
    if score >= 90:
        return "S", "强推荐"
    if score >= 75:
        return "A", "热点项目"
    if score >= 60:
        return "B", "值得关注"
    if score >= 40:
        return "C", "仅归档"
    return "D", "排除"


def _rank_reason(
    profile: GitHubProjectProfile,
    digest: GitHubProjectDigest,
    popularity: int,
    activity: int,
    quality: int,
    risk_score: int,
) -> str:
    return (
        f"AI 相关度 {digest.ai_relevance_score}，热度 {popularity}，活跃度 {activity}，"
        f"项目质量 {quality}，可用性 {digest.usability_score}，风险 {risk_score}。"
        f"stars={profile.stars}, forks={profile.forks}, pushed_at={profile.pushed_at or 'unknown'}。"
    )


def _log_score(value: int, *, max_value: int) -> float:
    value = max(0, value)
    return min(math.log10(value + 1) / math.log10(max_value + 1) * 100, 100)


def _days_since(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max((now - parsed.astimezone(timezone.utc)).days, 0)


def _clamp(value: int) -> int:
    return max(0, min(value, 100))
