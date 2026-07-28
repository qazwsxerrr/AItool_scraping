from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATABASE_URL = "sqlite:///./data/ai_tool_intel.db"
DEFAULT_USER_AGENT = "AItool_scraping/0.1 (+https://example.local)"


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs from a local .env file without overriding env."""
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
    github_api_token: str | None = None
    github_api_version: str = "2022-11-28"
    github_timeout_seconds: float = 20.0
    ai_review_api_url: str | None = None
    ai_review_api_key: str | None = None
    ai_review_model: str | None = None
    ai_review_api_style: str = "generic_json"
    ai_review_timeout_seconds: float = 30.0
    ai_review_min_candidate_score: int = 70
    claim_extract_api_url: str | None = None
    claim_extract_api_key: str | None = None
    claim_extract_model: str | None = None
    claim_extract_api_style: str = "generic_json"
    claim_extract_timeout_seconds: float = 30.0
    claim_extract_min_ai_score: int = 70
    tavily_base_url: str = "https://api.tavily.com"
    tavily_api_key: str | None = None
    tavily_search_depth: str = "basic"
    tavily_max_results: int = 5
    tavily_include_raw_content: bool = False
    tavily_timeout_seconds: float = 20.0
    evidence_search_max_attempts: int = 3
    evidence_search_cache_ttl_hours: int = 24
    evidence_fetch_timeout_seconds: float = 20.0
    evidence_fetch_max_bytes: int = 524288
    ai_verify_api_url: str | None = None
    ai_verify_api_key: str | None = None
    ai_verify_model: str | None = None
    ai_verify_api_style: str = "generic_json"
    ai_verify_timeout_seconds: float = 60.0
    final_review_min_score: int = 75
    final_review_min_credibility: int = 60
    final_review_max_spam_risk: int = 40

    @classmethod
    def from_env(cls, dotenv_path: str | Path = ".env") -> "Settings":
        load_dotenv(dotenv_path)
        rsshub_base_url = os.getenv("RSSHUB_BASE_URL") or None
        if rsshub_base_url:
            rsshub_base_url = rsshub_base_url.rstrip("/")

        return cls(
            database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
            rsshub_base_url=rsshub_base_url,
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            request_retries=int(os.getenv("REQUEST_RETRIES", "2")),
            user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
            github_api_base_url=os.getenv("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/"),
            github_api_token=os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN") or None,
            github_api_version=os.getenv("GITHUB_API_VERSION", "2022-11-28"),
            github_timeout_seconds=float(os.getenv("GITHUB_TIMEOUT_SECONDS", "20")),
            ai_review_api_url=os.getenv("AI_REVIEW_API_URL") or None,
            ai_review_api_key=os.getenv("AI_REVIEW_API_KEY") or None,
            ai_review_model=os.getenv("AI_REVIEW_MODEL") or None,
            ai_review_api_style=os.getenv("AI_REVIEW_API_STYLE", "generic_json"),
            ai_review_timeout_seconds=float(os.getenv("AI_REVIEW_TIMEOUT_SECONDS", "30")),
            ai_review_min_candidate_score=int(os.getenv("AI_REVIEW_MIN_CANDIDATE_SCORE", "70")),
            claim_extract_api_url=os.getenv("CLAIM_EXTRACT_API_URL") or os.getenv("AI_REVIEW_API_URL") or None,
            claim_extract_api_key=os.getenv("CLAIM_EXTRACT_API_KEY") or os.getenv("AI_REVIEW_API_KEY") or None,
            claim_extract_model=os.getenv("CLAIM_EXTRACT_MODEL") or os.getenv("AI_REVIEW_MODEL") or None,
            claim_extract_api_style=os.getenv("CLAIM_EXTRACT_API_STYLE", os.getenv("AI_REVIEW_API_STYLE", "generic_json")),
            claim_extract_timeout_seconds=float(os.getenv("CLAIM_EXTRACT_TIMEOUT_SECONDS", "30")),
            claim_extract_min_ai_score=int(os.getenv("CLAIM_EXTRACT_MIN_AI_SCORE", "70")),
            tavily_base_url=os.getenv("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/"),
            tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
            tavily_search_depth=os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
            tavily_max_results=int(os.getenv("TAVILY_MAX_RESULTS", "5")),
            tavily_include_raw_content=_env_bool("TAVILY_INCLUDE_RAW_CONTENT", False),
            tavily_timeout_seconds=float(os.getenv("TAVILY_TIMEOUT_SECONDS", "20")),
            evidence_search_max_attempts=int(os.getenv("EVIDENCE_SEARCH_MAX_ATTEMPTS", "3")),
            evidence_search_cache_ttl_hours=int(os.getenv("EVIDENCE_SEARCH_CACHE_TTL_HOURS", "24")),
            evidence_fetch_timeout_seconds=float(os.getenv("EVIDENCE_FETCH_TIMEOUT_SECONDS", "20")),
            evidence_fetch_max_bytes=int(os.getenv("EVIDENCE_FETCH_MAX_BYTES", "524288")),
            ai_verify_api_url=os.getenv("AI_VERIFY_API_URL") or os.getenv("AI_REVIEW_API_URL") or None,
            ai_verify_api_key=os.getenv("AI_VERIFY_API_KEY") or os.getenv("AI_REVIEW_API_KEY") or None,
            ai_verify_model=os.getenv("AI_VERIFY_MODEL") or os.getenv("AI_REVIEW_MODEL") or None,
            ai_verify_api_style=os.getenv("AI_VERIFY_API_STYLE", os.getenv("AI_REVIEW_API_STYLE", "generic_json")),
            ai_verify_timeout_seconds=float(os.getenv("AI_VERIFY_TIMEOUT_SECONDS", "60")),
            final_review_min_score=int(os.getenv("FINAL_REVIEW_MIN_SCORE", "75")),
            final_review_min_credibility=int(os.getenv("FINAL_REVIEW_MIN_CREDIBILITY", "60")),
            final_review_max_spam_risk=int(os.getenv("FINAL_REVIEW_MAX_SPAM_RISK", "40")),
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
