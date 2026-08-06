"""Read GitHub project metadata exported by the v2 intelligence pipeline.

GitHub repositories are not scored by AI and do not use a second report
pipeline. This reader only filters and orders observable repository metrics
from ``intel_items.jsonl``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dateutil import parser as date_parser


INTEL_EXPORT_FILENAME = "intel_items.jsonl"
DEFAULT_OUTPUT_ROOT = Path("output")


@dataclass(frozen=True)
class GitHubProjectFilters:
    query: str | None = None
    language: str | None = None
    min_stars: int | None = None
    min_forks: int | None = None
    include_archived: bool = True


@dataclass(frozen=True)
class GitHubProjectRow:
    intel_item_id: int | None
    repo_full_name: str
    url: str | None
    source_id: str | None
    status: str | None
    summary: str | None
    stars: int
    forks: int
    watchers: int
    open_issues: int
    primary_language: str | None
    license_name: str | None
    topics: list[str]
    archived: bool
    fork: bool
    pushed_at: datetime | None
    published_at: datetime | None
    latest_release_names: list[str]
    risk_flags: list[str]
    trending: dict[str, dict[str, Any]]
    search_topics: list[str]
    discovery_sources: list[str]

    @property
    def display_title(self) -> str:
        return self.repo_full_name or "unknown/repository"

    @property
    def is_risky(self) -> bool:
        return bool(self.risk_flags)


@dataclass(frozen=True)
class GitHubProjectStats:
    total: int
    active_count: int
    archived_count: int
    risky_count: int
    max_stars: int
    data_path: Path | None
    updated_at: datetime | None


@dataclass(frozen=True)
class GitHubProjectList:
    rows: list[GitHubProjectRow]
    stats: GitHubProjectStats


class GitHubProjectReader:
    """Read and rank GitHub rows using only persisted source metadata."""

    def __init__(
        self,
        data_path: str | Path | None = None,
        *,
        output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    ) -> None:
        self.data_path = Path(data_path) if data_path else None
        self.output_root = Path(output_root)

    def list_projects(
        self,
        *,
        filters: GitHubProjectFilters | None = None,
        limit: int | None = None,
    ) -> GitHubProjectList:
        filters = filters or GitHubProjectFilters()
        path = self.resolve_data_path()
        if path is None:
            return GitHubProjectList(rows=[], stats=self._stats([], None))

        rows = [row for row in self._read_rows(path) if _matches_filters(row, filters)]
        rows.sort(key=_sort_key, reverse=True)
        stats = self._stats(rows, path)
        if limit is not None:
            rows = rows[: max(0, limit)]
        return GitHubProjectList(rows=rows, stats=stats)

    def search(self, query: str, *, limit: int = 8) -> list[GitHubProjectRow]:
        normalized = query.strip()
        if not normalized:
            return []
        return self.list_projects(
            filters=GitHubProjectFilters(query=normalized),
            limit=limit,
        ).rows

    def resolve_data_path(self) -> Path | None:
        if self.data_path:
            if self.data_path.is_dir():
                candidate = self.data_path / INTEL_EXPORT_FILENAME
                return candidate if candidate.is_file() else None
            return self.data_path if self.data_path.is_file() else None

        if not self.output_root.exists():
            return None
        direct = self.output_root / INTEL_EXPORT_FILENAME
        if direct.is_file():
            return direct
        matches = [path for path in self.output_root.glob(f"**/{INTEL_EXPORT_FILENAME}") if path.is_file()]
        if not matches:
            return None
        return max(matches, key=lambda path: path.stat().st_mtime)

    def _read_rows(self, path: Path) -> list[GitHubProjectRow]:
        rows: list[GitHubProjectRow] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return rows
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                row = _row_from_record(record)
                if row is not None:
                    rows.append(row)
        return rows

    def _stats(self, rows: list[GitHubProjectRow], path: Path | None) -> GitHubProjectStats:
        updated_at = None
        if path is not None:
            try:
                updated_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                pass
        return GitHubProjectStats(
            total=len(rows),
            active_count=sum(1 for row in rows if not row.archived),
            archived_count=sum(1 for row in rows if row.archived),
            risky_count=sum(1 for row in rows if row.is_risky),
            max_stars=max((row.stars for row in rows), default=0),
            data_path=path,
            updated_at=updated_at,
        )


def _row_from_record(record: dict[str, Any]) -> GitHubProjectRow | None:
    if record.get("content_class") != "project_tool" or not _is_github_record(record):
        return None

    metrics = _mapping(record.get("metrics"))
    payload = _mapping(record.get("raw_payload"))
    # Collector metrics are the canonical refresh, but an absent metric must
    # not erase a useful value retained in the raw GitHub response.
    merged = dict(payload)
    merged.update({key: value for key, value in metrics.items() if value is not None})
    trending = _trending_signals(merged)
    url = _text(record.get("url") or merged.get("html_url"))
    repo_full_name = (
        _text(merged.get("full_name"))
        or _text(metrics.get("canonical_project_key"))
        or _repo_name_from_url(url)
        or _repo_name_from_title(_text(record.get("title")))
        or "unknown/repository"
    )
    license_name = _license_name(merged.get("license")) or _text(merged.get("license_name"))
    archived = _truthy(merged.get("archived"))
    fork = _truthy(merged.get("fork") or merged.get("is_fork"))
    readme_checked = merged.get("readme_checked") is True
    has_readme = merged.get("has_readme", merged.get("readme_present"))
    risk_flags: list[str] = []
    if archived:
        risk_flags.append("archived_repository")
    if fork:
        risk_flags.append("fork_repository")
    if license_name is None:
        risk_flags.append("missing_license")
    if readme_checked and has_readme is False:
        risk_flags.append("missing_readme")

    return GitHubProjectRow(
        intel_item_id=_int_or_none(record.get("id")),
        repo_full_name=repo_full_name,
        url=url,
        source_id=_text(record.get("source_id")),
        status=_text(record.get("status")),
        summary=_text(record.get("summary") or record.get("content_text")),
        stars=_number(merged.get("stars") or merged.get("stargazers_count")),
        forks=_number(merged.get("forks") or merged.get("forks_count")),
        watchers=_number(merged.get("watchers") or merged.get("watchers_count")),
        open_issues=_number(merged.get("open_issues") or merged.get("open_issues_count")),
        primary_language=_text(merged.get("language")),
        license_name=license_name,
        topics=_string_list(merged.get("topics")),
        archived=archived,
        fork=fork,
        pushed_at=_parse_datetime(merged.get("pushed_at") or merged.get("updated_at")),
        published_at=_parse_datetime(record.get("published_at")),
        latest_release_names=_release_names(
            merged.get("latest_releases")
            or merged.get("latest_release")
            or merged.get("tag_name")
        ),
        risk_flags=risk_flags,
        trending=trending,
        search_topics=_string_list(merged.get("search_topics")),
        discovery_sources=_string_list(merged.get("discovery_sources")),
    )


def _is_github_record(record: dict[str, Any]) -> bool:
    source_id = (_text(record.get("source_id")) or "").casefold()
    source_type = (_text(record.get("source_type")) or "").casefold()
    external_id = (_text(record.get("external_id")) or "").casefold()
    url = (_text(record.get("url")) or "").casefold()
    metrics = _mapping(record.get("metrics"))
    payload = _mapping(record.get("raw_payload"))
    return bool(
        source_type in {"github_api", "github_trending"}
        or source_id.startswith("github_")
        or external_id.startswith("github_repo:")
        or "github.com/" in url
        or payload.get("github_item_type") in {"repository", "release"}
        or metrics.get("canonical_project_key")
    )


def _matches_filters(row: GitHubProjectRow, filters: GitHubProjectFilters) -> bool:
    if filters.query and filters.query.casefold() not in _search_text(row):
        return False
    if filters.language and (row.primary_language or "").casefold() != filters.language.casefold():
        return False
    if filters.min_stars is not None and row.stars < filters.min_stars:
        return False
    if filters.min_forks is not None and row.forks < filters.min_forks:
        return False
    if not filters.include_archived and row.archived:
        return False
    return True


def _search_text(row: GitHubProjectRow) -> str:
    return " ".join(
        value
        for value in (
            row.repo_full_name,
            row.summary,
            row.primary_language,
            row.license_name,
            *row.topics,
            *row.search_topics,
        )
        if value
    ).casefold()


def _sort_key(row: GitHubProjectRow) -> tuple[int, int, int, int, float, int]:
    pushed = row.pushed_at.timestamp() if row.pushed_at else float("-inf")
    weekly = _number(row.trending.get("weekly", {}).get("stars_since"))
    daily = _number(row.trending.get("daily", {}).get("stars_since"))
    return weekly, daily, row.stars, row.forks, pushed, -(row.intel_item_id or 0)


def _trending_signals(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = metrics.get("trending")
    result = {key: dict(value) for key, value in raw.items() if isinstance(value, dict)} if isinstance(raw, dict) else {}
    period = _text(metrics.get("trending_period"))
    if period and period not in result:
        result[period] = {
            "rank": metrics.get("trending_rank"),
            "stars_since": metrics.get("stars_since"),
            "stars": metrics.get("stars"),
            "forks": metrics.get("forks"),
        }
    return result


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _repo_name_from_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if (parsed.hostname or "").casefold().removeprefix("www.") != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def _repo_name_from_title(value: str | None) -> str | None:
    if not value:
        return None
    text = value.removeprefix("GitHub repo:").strip()
    return text if "/" in text else None


def _license_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("spdx_id") or value.get("name"))
    return _text(value)


def _release_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for entry in value[:3]:
        if isinstance(entry, dict):
            name = _text(entry.get("name") or entry.get("tag_name"))
        else:
            name = _text(entry)
        if name:
            result.append(name)
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(entry) for entry in value if entry is not None]


def _number(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        text = str(value).strip().casefold().replace(",", "")
        multiplier = 1
        if text.endswith("k"):
            multiplier, text = 1_000, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1_000_000, text[:-1]
        return max(0, int(float(text) * multiplier))
    except (TypeError, ValueError):
        return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y"}
    return bool(value)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date_parser.parse(text)
    except (TypeError, ValueError, OverflowError):
        return None


__all__ = [
    "GitHubProjectFilters",
    "GitHubProjectList",
    "GitHubProjectReader",
    "GitHubProjectRow",
    "GitHubProjectStats",
]
