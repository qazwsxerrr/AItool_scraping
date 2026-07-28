from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config.settings import DEFAULT_USER_AGENT, Settings
from app.github.project_types import GitHubProjectProfile


class GitHubProjectEnricher:
    """Fetch GitHub project metadata needed for project intelligence reports."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        token: str | None = None,
        api_version: str = "2022-11-28",
        timeout_seconds: float = 20.0,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> "GitHubProjectEnricher":
        return cls(
            base_url=settings.github_api_base_url,
            token=settings.github_api_token,
            api_version=settings.github_api_version,
            timeout_seconds=settings.github_timeout_seconds,
            user_agent=settings.user_agent,
        )

    def enrich(self, base_profile: GitHubProjectProfile) -> GitHubProjectProfile:
        owner, repo = _owner_repo(base_profile)
        if not owner or not repo:
            return _replace_profile(base_profile, profile_errors=[*base_profile.profile_errors, "无法解析 owner/repo"])

        errors: list[str] = [*base_profile.profile_errors]
        repo_data: dict[str, Any] = {
            "full_name": base_profile.repo_full_name,
            "html_url": base_profile.url,
            "description": base_profile.description,
            "homepage": base_profile.homepage,
            "topics": base_profile.topics,
            "language": base_profile.primary_language,
            "stargazers_count": base_profile.stars,
            "forks_count": base_profile.forks,
            "watchers_count": base_profile.watchers,
            "open_issues_count": base_profile.open_issues,
            "license": base_profile.license_name,
            "archived": base_profile.archived,
            "fork": base_profile.fork,
            "created_at": base_profile.created_at,
            "updated_at": base_profile.updated_at,
            "pushed_at": base_profile.pushed_at,
        }
        readme_text = base_profile.readme_text
        readme_excerpt = base_profile.readme_excerpt
        languages = dict(base_profile.languages)
        releases = list(base_profile.latest_releases)

        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, headers=self._headers(), transport=self.transport) as client:
            repo_response = _safe_get(client, f"{self.base_url}/repos/{owner}/{repo}", errors, "repo_detail")
            if isinstance(repo_response, dict):
                repo_data = {**repo_data, **repo_response}

            languages_response = _safe_get(client, f"{self.base_url}/repos/{owner}/{repo}/languages", errors, "languages")
            if isinstance(languages_response, dict):
                languages = {str(key): int(value) for key, value in languages_response.items() if isinstance(value, int)}

            releases_response = _safe_get(
                client,
                f"{self.base_url}/repos/{owner}/{repo}/releases",
                errors,
                "releases",
                params={"per_page": "3"},
            )
            if isinstance(releases_response, list):
                releases = [_release_summary(item) for item in releases_response if isinstance(item, dict)]

            readme_response = _safe_get(client, f"{self.base_url}/repos/{owner}/{repo}/readme", errors, "readme")
            if isinstance(readme_response, dict):
                readme_text = _decode_readme(readme_response) or readme_text
                readme_excerpt = _excerpt(readme_text) if readme_text else readme_excerpt

        return GitHubProjectProfile(
            normalized_item_id=base_profile.normalized_item_id,
            raw_item_id=base_profile.raw_item_id,
            source_id=base_profile.source_id,
            title=base_profile.title,
            url=_text(repo_data.get("html_url")) or base_profile.url,
            published_at=base_profile.published_at,
            repo_full_name=_text(repo_data.get("full_name")) or base_profile.repo_full_name,
            owner=owner,
            repo=repo,
            description=_text(repo_data.get("description")) or base_profile.description,
            homepage=_text(repo_data.get("homepage")) or base_profile.homepage,
            topics=_string_list(repo_data.get("topics")) or base_profile.topics,
            primary_language=_text(repo_data.get("language")) or base_profile.primary_language,
            languages=languages,
            stars=_int(repo_data.get("stargazers_count"), base_profile.stars),
            forks=_int(repo_data.get("forks_count"), base_profile.forks),
            watchers=_int(repo_data.get("watchers_count"), base_profile.watchers),
            open_issues=_int(repo_data.get("open_issues_count"), base_profile.open_issues),
            license_name=_license_name(repo_data.get("license")) or base_profile.license_name,
            archived=bool(repo_data.get("archived", base_profile.archived)),
            fork=bool(repo_data.get("fork", base_profile.fork)),
            created_at=_text(repo_data.get("created_at")) or base_profile.created_at,
            updated_at=_text(repo_data.get("updated_at")) or base_profile.updated_at,
            pushed_at=_text(repo_data.get("pushed_at")) or base_profile.pushed_at,
            readme_excerpt=readme_excerpt,
            readme_text=readme_text,
            latest_releases=releases,
            profile_errors=errors,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": self.api_version,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def profile_from_row(row: dict[str, Any]) -> GitHubProjectProfile:
    payload = _loads_raw_payload_dict(row.get("raw_payload"))
    full_name = _text(payload.get("full_name")) or _repo_full_name_from_url(_text(row.get("url"))) or _text(row.get("title")) or "unknown/repository"
    owner, repo = _split_full_name(full_name)
    return GitHubProjectProfile(
        normalized_item_id=int(row["normalized_item_id"]),
        raw_item_id=int(row["raw_item_id"]),
        source_id=str(row["source_id"]),
        title=str(row["title"]),
        url=_text(row.get("url")) or _text(payload.get("html_url")),
        published_at=_text(row.get("published_at")),
        repo_full_name=full_name,
        owner=owner,
        repo=repo,
        description=_text(payload.get("description")) or _text(row.get("raw_summary")),
        homepage=_text(payload.get("homepage")),
        topics=_string_list(payload.get("topics")),
        primary_language=_text(payload.get("language")),
        stars=_int(payload.get("stargazers_count")),
        forks=_int(payload.get("forks_count")),
        watchers=_int(payload.get("watchers_count")),
        open_issues=_int(payload.get("open_issues_count")),
        license_name=_license_name(payload.get("license")),
        archived=bool(payload.get("archived", False)),
        fork=bool(payload.get("fork", False)),
        created_at=_text(payload.get("created_at")),
        updated_at=_text(payload.get("updated_at")),
        pushed_at=_text(payload.get("pushed_at")),
    )


def _safe_get(
    client: httpx.Client,
    url: str,
    errors: list[str],
    label: str,
    *,
    params: dict[str, str] | None = None,
) -> Any | None:
    try:
        response = client.get(url, params=params)
        if response.status_code == 404:
            errors.append(f"{label}: 404")
            return None
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        errors.append(f"{label}: {exc}")
        return None


def _decode_readme(data: dict[str, Any]) -> str | None:
    content = data.get("content")
    encoding = data.get("encoding")
    if not isinstance(content, str) or encoding != "base64":
        return None
    try:
        return base64.b64decode(content).decode("utf-8", errors="replace")
    except Exception:
        return None


def _excerpt(value: str | None, max_chars: int = 5000) -> str | None:
    if not value:
        return None
    text = "\n".join(line.rstrip() for line in value.splitlines()).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _release_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "tag_name": item.get("tag_name"),
        "published_at": item.get("published_at"),
        "html_url": item.get("html_url"),
        "body_excerpt": _excerpt(_text(item.get("body")), max_chars=800),
    }


def _loads_raw_payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _owner_repo(profile: GitHubProjectProfile) -> tuple[str | None, str | None]:
    if profile.owner and profile.repo:
        return profile.owner, profile.repo
    owner, repo = _split_full_name(profile.repo_full_name)
    if owner and repo:
        return owner, repo
    return _split_full_name(_repo_full_name_from_url(profile.url) or "")


def _repo_full_name_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def _split_full_name(value: str | None) -> tuple[str | None, str | None]:
    if not value or "/" not in value:
        return None, None
    owner, repo = value.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    return (owner or None), (repo or None)


def _replace_profile(profile: GitHubProjectProfile, *, profile_errors: list[str]) -> GitHubProjectProfile:
    return GitHubProjectProfile(**{**profile.__dict__, "profile_errors": profile_errors})


def _license_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("spdx_id")) or _text(value.get("name"))
    return _text(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
