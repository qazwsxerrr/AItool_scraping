from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.categories import DEFAULT_TOPIC_CATEGORIES
from app.config.limits import (
    DEFAULT_AI_ANALYSIS_MIN_SCORE,
    DEFAULT_AI_REVIEW_CONCURRENCY,
    DEFAULT_AI_SCREEN_REJECT_THRESHOLD,
    DEFAULT_STAGE_B_RESERVE_LIMIT,
    DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS,
    DEFAULT_STAGE_C_AGENT_MAX_TURNS,
    DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES,
)


DEFAULT_DATABASE_URL = "sqlite:///./data/ai_tool_intel.db"
DEFAULT_USER_AGENT = "AItool_scraping/0.1 (+https://example.local)"
DEFAULT_AI_REVIEW_CATEGORIES = DEFAULT_TOPIC_CATEGORIES


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    rsshub_base_url: str | None = None
    request_timeout_seconds: float = 20.0
    request_retries: int = 2
    user_agent: str = DEFAULT_USER_AGENT
    github_api_base_url: str = "https://api.github.com"
    github_api_token: str | None = field(default=None, repr=False)
    github_api_version: str = "2022-11-28"
    github_timeout_seconds: float = 20.0
    ai_review_api_url: str | None = None
    ai_review_api_key: str | None = field(default=None, repr=False)
    ai_review_model: str | None = None
    ai_review_timeout_seconds: float = 30.0
    ai_screen_reject_threshold: int = DEFAULT_AI_SCREEN_REJECT_THRESHOLD
    ai_analysis_min_score: int = DEFAULT_AI_ANALYSIS_MIN_SCORE
    ai_review_concurrency: int = DEFAULT_AI_REVIEW_CONCURRENCY
    ai_review_categories: tuple[str, ...] = DEFAULT_AI_REVIEW_CATEGORIES
    stage_b_reserve_limit: int = DEFAULT_STAGE_B_RESERVE_LIMIT
    stage_c_timeout_seconds: float = 120.0
    stage_c_agent_max_turns: int = DEFAULT_STAGE_C_AGENT_MAX_TURNS
    stage_c_agent_max_tool_calls: int = DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS
    stage_c_agent_max_web_searches: int = DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES
    stage_c_trusted_domains: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> "Settings":
        load_dotenv(dotenv_path)
        rsshub_base_url = _env_value("RSSHUB_BASE_URL")
        ai_review_model = os.getenv("AI_REVIEW_MODEL") or None
        categories = _parse_categories(os.getenv("AI_REVIEW_CATEGORIES"))
        trusted_domains = _parse_domains(os.getenv("AI_STAGE_C_TRUSTED_DOMAINS"))
        return cls(
            database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
            rsshub_base_url=rsshub_base_url.rstrip("/") if rsshub_base_url else None,
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            request_retries=int(os.getenv("REQUEST_RETRIES", "2")),
            user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
            github_api_base_url=os.getenv("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/"),
            github_api_token=os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN") or None,
            github_api_version=os.getenv("GITHUB_API_VERSION", "2022-11-28"),
            github_timeout_seconds=float(os.getenv("GITHUB_TIMEOUT_SECONDS", "20")),
            ai_review_api_url=os.getenv("AI_REVIEW_API_URL") or None,
            ai_review_api_key=os.getenv("AI_REVIEW_API_KEY") or None,
            ai_review_model=ai_review_model,
            ai_review_timeout_seconds=float(os.getenv("AI_REVIEW_TIMEOUT_SECONDS", "30")),
            ai_screen_reject_threshold=_bounded_int(
                os.getenv("AI_SCREEN_REJECT_THRESHOLD"), DEFAULT_AI_SCREEN_REJECT_THRESHOLD
            ),
            ai_analysis_min_score=_bounded_int(
                os.getenv("AI_ANALYSIS_MIN_SCORE"), DEFAULT_AI_ANALYSIS_MIN_SCORE
            ),
            ai_review_concurrency=max(
                1,
                _bounded_int(os.getenv("AI_REVIEW_CONCURRENCY"), DEFAULT_AI_REVIEW_CONCURRENCY),
            ),
            ai_review_categories=categories,
            stage_b_reserve_limit=_positive_int(
                os.getenv("AI_STAGE_B_RESERVE_LIMIT"), DEFAULT_STAGE_B_RESERVE_LIMIT, maximum=100
            ),
            stage_c_timeout_seconds=float(os.getenv("AI_STAGE_C_TIMEOUT_SECONDS", "120")),
            stage_c_agent_max_turns=_positive_int(
                os.getenv("AI_STAGE_C_AGENT_MAX_TURNS"), DEFAULT_STAGE_C_AGENT_MAX_TURNS, maximum=None
            ),
            stage_c_agent_max_tool_calls=_positive_int(
                os.getenv("AI_STAGE_C_AGENT_MAX_TOOL_CALLS"), DEFAULT_STAGE_C_AGENT_MAX_TOOL_CALLS, maximum=None
            ),
            stage_c_agent_max_web_searches=_nonnegative_int(
                os.getenv("AI_STAGE_C_AGENT_MAX_WEB_SEARCHES"), DEFAULT_STAGE_C_AGENT_MAX_WEB_SEARCHES, maximum=None
            ),
            stage_c_trusted_domains=trusted_domains,
        )


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _parse_categories(value: str | None) -> tuple[str, ...]:
    if not value:
        return DEFAULT_AI_REVIEW_CATEGORIES
    parts = [part.strip() for part in value.replace("，", ",").split(",") if part.strip()]
    # Stage B1 has one fixed six-label editorial taxonomy.  Ignore removed or
    # ad-hoc labels instead of allowing configuration to reintroduce them.
    allowed = set(DEFAULT_AI_REVIEW_CATEGORIES)
    unique = tuple(dict.fromkeys(part for part in parts if part in allowed))
    return unique or DEFAULT_AI_REVIEW_CATEGORIES


def _bounded_int(value: str | None, default: int) -> int:
    try:
        return max(0, min(100, int(str(value).strip()))) if value is not None else default
    except (TypeError, ValueError):
        return default


def _positive_int(value: str | None, default: int, *, maximum: int | None) -> int:
    try:
        parsed = int(str(value).strip()) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(maximum, parsed)) if maximum is not None else max(1, parsed)


def _nonnegative_int(value: str | None, default: int, *, maximum: int | None) -> int:
    try:
        parsed = int(str(value).strip()) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(maximum, parsed)) if maximum is not None else max(0, parsed)


def _parse_domains(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    values: list[str] = []
    for raw in value.replace("，", ",").split(","):
        host = raw.strip().casefold().removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        if host and host not in values:
            values.append(host)
    return tuple(values)
