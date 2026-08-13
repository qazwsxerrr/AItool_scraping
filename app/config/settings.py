from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_DATABASE_URL = "sqlite:///./data/ai_tool_intel.db"
DEFAULT_USER_AGENT = "AItool_scraping/0.1 (+https://example.local)"


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
    ai_review_api_style: str = "generic_json"
    ai_review_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> "Settings":
        load_dotenv(dotenv_path)
        rsshub_base_url = _env_value("RSSHUB_BASE_URL")
        ai_review_model = os.getenv("AI_REVIEW_MODEL") or None
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
            ai_review_api_style=os.getenv("AI_REVIEW_API_STYLE", "generic_json"),
            ai_review_timeout_seconds=float(os.getenv("AI_REVIEW_TIMEOUT_SECONDS", "30")),
        )


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None
