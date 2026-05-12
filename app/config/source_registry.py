from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

LOGGER = logging.getLogger(__name__)
ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("source_registry.yaml")


class SourceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: Literal["rss", "atom", "rsshub"]
    url: str = Field(min_length=1)
    enabled: bool = True
    priority: int = 100
    fetch_interval: int = 3600
    parser_type: Literal["feedparser"] = "feedparser"
    source_group: str = "general"
    source_subtype: str = "fixed"
    quality_weight: float | None = None
    source_role: Literal["official", "community", "launch_platform", "social", "forum", "search", "code_hosting", "unknown"] | None = None
    spam_risk: Literal["low", "medium", "high"] | None = None
    requires_verification: bool | None = None
    default_limit: int = 30
    search_query: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", value):
            raise ValueError("id must contain lowercase letters, numbers, underscore or dash")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute http(s) URL")
        return value

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, value: int) -> int:
        if value < 0:
            raise ValueError("priority must be non-negative")
        return value

    @field_validator("fetch_interval")
    @classmethod
    def validate_fetch_interval(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("fetch_interval must be positive")
        return value

    @field_validator("source_group", "source_subtype")
    @classmethod
    def validate_source_metadata(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", value):
            raise ValueError("source metadata must contain lowercase letters, numbers, underscore or dash")
        return value

    @field_validator("default_limit")
    @classmethod
    def validate_default_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("default_limit must be positive")
        return value

    @field_validator("quality_weight")
    @classmethod
    def validate_quality_weight(cls, value: float | None) -> float | None:
        if value is None:
            return value
        if value < 0 or value > 1:
            raise ValueError("quality_weight must be between 0 and 1")
        return value


@dataclass(frozen=True)
class SkippedSource:
    source_id: str
    reason: str


@dataclass(frozen=True)
class RegistryLoadResult:
    sources: list[SourceConfig]
    skipped: list[SkippedSource]


def _interpolate_env(value: str, env: Mapping[str, str]) -> tuple[str | None, str | None]:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        replacement = env.get(key) or os.environ.get(key)
        if not replacement:
            missing.append(key)
            return match.group(0)
        if key.endswith("BASE_URL"):
            replacement = replacement.rstrip("/")
        return replacement

    interpolated = ENV_PATTERN.sub(replace, value)
    if missing:
        return None, f"missing env: {', '.join(sorted(set(missing)))}"
    return interpolated, None


def load_source_registry(
    path: str | Path = DEFAULT_REGISTRY_PATH,
    *,
    env: Mapping[str, str] | None = None,
) -> RegistryLoadResult:
    """Load enabled source configs and skip env-gated RSSHub sources safely."""
    registry_path = Path(path)
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    raw_sources = raw.get("sources", [])
    if not isinstance(raw_sources, list):
        raise ValueError("source_registry.yaml must contain a list under 'sources'")

    env_mapping = env or {}
    sources: list[SourceConfig] = []
    skipped: list[SkippedSource] = []

    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise ValueError("each source entry must be a mapping")

        source_id = str(raw_source.get("id", "<unknown>"))
        if raw_source.get("enabled", True) is False:
            continue

        url = str(raw_source.get("url", ""))
        interpolated_url, skip_reason = _interpolate_env(url, env_mapping)
        if skip_reason:
            reason = f"{skip_reason}; source requires configured template URL"
            LOGGER.warning("Skipping source %s: %s", source_id, reason)
            skipped.append(SkippedSource(source_id=source_id, reason=reason))
            continue

        source_data: dict[str, Any] = dict(raw_source)
        source_data["url"] = interpolated_url
        source = SourceConfig.model_validate(source_data)
        sources.append(source)

    sources.sort(key=lambda item: (item.priority, item.id))
    return RegistryLoadResult(sources=sources, skipped=skipped)
