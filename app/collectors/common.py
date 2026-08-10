"""Pure value helpers shared by feed and GitHub collector modules."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

from dateutil import parser as date_parser


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = date_parser.parse(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def number(value: Any) -> int | float | None:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().casefold().replace(",", "")
            multiplier = 1.0
            if text.endswith("k"):
                multiplier, text = 1_000.0, text[:-1]
            elif text.endswith("m"):
                multiplier, text = 1_000_000.0, text[:-1]
            result = float(text) * multiplier
            return int(result) if result.is_integer() else result
        return int(value)
    except (TypeError, ValueError):
        return None


def text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def canonical_github_repo_url(value: Any) -> str | None:
    """Return a case-insensitive GitHub repository URL for stable dedupe."""

    value_text = text(value)
    if not value_text:
        return None
    repo_url = value_text if "://" in value_text else f"https://github.com/{value_text.lstrip('/')}"
    parsed = urlparse(repo_url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host not in {"github.com", "www.github.com"} or len(parts) < 2:
        return None
    owner, repo = parts[0].strip(), parts[1].removesuffix(".git").strip()
    if not owner or not repo:
        return None
    return f"https://github.com/{owner.casefold()}/{repo.casefold()}"


def owner_repo(url: str) -> tuple[str | None, str | None]:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 4 and parts[0] == "repos":
        return parts[1], parts[2]
    return None, None


def find_github_url(*values: Any) -> str | None:
    for value in values:
        if not value:
            continue
        match = re.search(
            r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
            str(value),
            flags=re.IGNORECASE,
        )
        if match:
            owner, repo = match.group(1), match.group(2).removesuffix(".git")
            return f"https://github.com/{owner}/{repo}".rstrip(".,)")
    return None


def github_owner_repo_from_html_url(url: str) -> tuple[str | None, str | None]:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2 or parts[0].casefold() in {"orgs", "users"}:
        return None, None
    return parts[0], parts[1].removesuffix(".git")


def merge_github_metrics(metrics: dict[str, Any], payload: dict[str, Any]) -> None:
    aliases = {
        "stars": "stargazers_count",
        "forks": "forks_count",
        "pushed_at": "pushed_at",
        "language": "language",
        "archived": "archived",
        "fork": "fork",
        "license": "license",
    }
    for target, key in aliases.items():
        value = payload.get(key)
        if target == "license" and isinstance(value, dict):
            value = value.get("spdx_id") or value.get("name")
        if value is not None:
            metrics[target] = value
    metrics["github_metadata_fetched"] = True


def copy_metric(metrics: dict[str, Any], payload: dict[str, Any], target: str, aliases: Iterable[str]) -> None:
    if target in metrics:
        return
    for alias in aliases:
        if payload.get(alias) is not None:
            value = payload[alias]
            if target in {"votes", "comments", "views", "likes", "reposts", "replies", "engagement"}:
                parsed = number(value)
                metrics[target] = parsed if parsed is not None else value
            else:
                metrics[target] = value
            return


def extract_feed_engagement(metrics: dict[str, Any], *, title: str | None, summary: str | None, content: str | None, payload: dict[str, Any]) -> None:
    """Recover engagement fields exposed by feed extensions or rendered text."""

    aliases = {
        "votes": ("votes", "votecount", "vote_count", "upvotes", "points", "ph_votes", "ph_vote_count"),
        "comments": ("comments", "commentcount", "comment_count", "comments_count", "ph_comments", "ph_comments_count", "ph_comment_count"),
    }
    flattened = {str(key).casefold().replace("-", "_"): value for key, value in payload.items()}
    for target, names in aliases.items():
        if target in metrics and metrics[target] is not None:
            continue
        for name in names:
            value = flattened.get(name)
            if value is not None:
                metrics[target] = number(value)
                break
    text_blob = " ".join(str(value) for value in (title, summary, content) if value)
    patterns = {
        "votes": r"(?:([0-9][0-9,]*)\s*(?:upvotes?|votes?)|(?:upvotes?|votes?)\s*[:：]?\s*([0-9][0-9,]*))",
        "comments": r"(?:([0-9][0-9,]*)\s*comments?|comments?\s*[:：]?\s*([0-9][0-9,]*))",
    }
    for target, pattern in patterns.items():
        if target in metrics:
            continue
        match = re.search(pattern, text_blob, flags=re.IGNORECASE)
        if match:
            raw = next((group for group in match.groups() if group), None)
            if raw is not None:
                metrics[target] = number(raw)


__all__ = [
    "canonical_github_repo_url",
    "copy_metric",
    "extract_feed_engagement",
    "find_github_url",
    "github_owner_repo_from_html_url",
    "merge_github_metrics",
    "number",
    "owner_repo",
    "parse_dt",
    "text",
]
