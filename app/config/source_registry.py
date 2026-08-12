from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml
from app.domain.models import CANONICAL_SOURCE_GROUPS, SourceSpec

LOGGER = logging.getLogger(__name__)
ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("source_registry.yaml")

# ``SourceSpec`` is the one runtime/configuration model.  Keep this import
# alias for callers that still import ``SourceConfig`` while migrating; it is
# intentionally not a second model with a legacy routing schema.
SourceConfig = SourceSpec


@dataclass(frozen=True)
class SkippedSource:
    source_id: str
    reason: str


@dataclass(frozen=True)
class RegistryLoadResult:
    sources: list[SourceSpec]
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
    sources: list[SourceSpec] = []
    skipped: list[SkippedSource] = []
    seen_ids: set[str] = set()

    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise ValueError("each source entry must be a mapping")

        source_id = str(raw_source.get("id", "<unknown>"))
        if source_id in seen_ids:
            raise ValueError(f"duplicate source id: {source_id}")
        seen_ids.add(source_id)
        if raw_source.get("enabled", True) is False:
            continue

        source_group = raw_source.get("source_group")
        if source_group is not None and source_group not in CANONICAL_SOURCE_GROUPS:
            raise ValueError(
                f"source {source_id} uses non-canonical source_group: {source_group}"
            )

        url = str(raw_source.get("url", ""))
        interpolated_url, skip_reason = _interpolate_env(url, env_mapping)
        if skip_reason:
            reason = f"{skip_reason}; source requires configured template URL"
            LOGGER.warning("Skipping source %s: %s", source_id, reason)
            skipped.append(SkippedSource(source_id=source_id, reason=reason))
            continue

        source_data: dict[str, Any] = dict(raw_source)
        source_data["url"] = interpolated_url
        source = SourceSpec.from_config(source_data)
        sources.append(source)

    sources.sort(key=lambda item: (item.priority, item.id))
    return RegistryLoadResult(sources=sources, skipped=skipped)
