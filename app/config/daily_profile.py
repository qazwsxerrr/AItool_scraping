"""Strict configuration model for the V3 daily edition profile."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SectionName = Literal[
    "model_product",
    "industry_infrastructure",
    "research",
    "open_source_tool",
    "practice_opinion",
]
DAILY_SECTIONS: tuple[SectionName, ...] = (
    "model_product",
    "industry_infrastructure",
    "research",
    "open_source_tool",
    "practice_opinion",
)
CANONICAL_PROFILE_GROUPS = {
    "github_trending",
    "github_release",
    "github_search",
    "producthunt",
    "reddit_fixed",
    "reddit_search",
    "linux_do",
    "x_official",
    "x_social",
    "x_search",
}
DEFAULT_REGISTRY_GROUP_CAPS = {
    "github_trending": 3,
    "github_release": 3,
    "github_search": 3,
    "producthunt": 1,
    "reddit_fixed": 2,
    "reddit_search": 0,
    "linux_do": 1,
    "x_official": 2,
    "x_social": 1,
    "x_search": 0,
}


class SectionProfile(BaseModel):
    """Target and minimum requirements for one editorial section."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: int = Field(ge=0)
    minimum_p1_primary: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _minimum_fits_target(self) -> "SectionProfile":
        if self.minimum_p1_primary > self.target:
            raise ValueError("minimum_p1_primary cannot exceed section target")
        return self


class DailyProfile(BaseModel):
    """Validated, immutable daily composition policy.

    The model intentionally uses a typed section mapping and rejects unknown
    sections/source groups.  GitHub's three source groups are configured for
    candidate-level visibility while ``aggregate_caps.github`` is the single
    publication cap shared by all of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_events: int = Field(default=20, ge=1)
    minimum_publishable_events: int = Field(default=14, ge=0)
    event_window_hours: int = Field(default=72, gt=0)
    max_per_source: int = Field(default=2, gt=0)
    max_per_event: int = Field(default=1, gt=0)
    sections: dict[SectionName, SectionProfile]
    source_group_caps: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_REGISTRY_GROUP_CAPS))
    aggregate_caps: dict[str, int] = Field(default_factory=lambda: {"github": 3})

    @field_validator("source_group_caps", "aggregate_caps")
    @classmethod
    def _validate_caps(cls, value: Mapping[str, int], info):
        caps = dict(value)
        for key, cap in caps.items():
            if not isinstance(key, str) or not key:
                raise ValueError("source group cap keys must be non-empty strings")
            if not isinstance(cap, int) or cap < 0:
                raise ValueError("source group caps must be non-negative integers")
        if info.field_name == "source_group_caps":
            unknown = set(caps) - CANONICAL_PROFILE_GROUPS
            if unknown:
                raise ValueError(f"unknown source group caps: {', '.join(sorted(unknown))}")
        else:
            unknown = set(caps) - {"github"}
            if unknown:
                raise ValueError(f"unknown aggregate caps: {', '.join(sorted(unknown))}")
        return caps

    @model_validator(mode="after")
    def _validate_profile(self) -> "DailyProfile":
        if self.minimum_publishable_events > self.target_events:
            raise ValueError("minimum_publishable_events cannot exceed target_events")
        missing = set(DAILY_SECTIONS) - set(self.sections)
        if missing:
            raise ValueError(f"missing daily sections: {', '.join(sorted(missing))}")
        extra = set(self.sections) - set(DAILY_SECTIONS)
        if extra:
            raise ValueError(f"unknown daily sections: {', '.join(sorted(extra))}")
        section_total = sum(section.target for section in self.sections.values())
        if section_total != self.target_events:
            raise ValueError(
                f"section target total ({section_total}) must equal target_events ({self.target_events})"
            )
        github_cap = self.aggregate_caps.get("github")
        if github_cap is None:
            raise ValueError("aggregate_caps must define github")
        subgroup_caps = [
            self.source_group_caps[group]
            for group in ("github_trending", "github_release", "github_search")
            if group in self.source_group_caps
        ]
        if subgroup_caps and github_cap > sum(subgroup_caps):
            raise ValueError("aggregate GitHub cap cannot exceed subgroup cap total")
        return self

    @property
    def github_aggregate_cap(self) -> int:
        """The one publication cap shared by all GitHub source groups."""

        return self.aggregate_caps["github"]

    @property
    def aggregate_source_group_caps(self) -> dict[str, int]:
        """Compatibility accessor used by quota composition code."""

        return dict(self.aggregate_caps)


DEFAULT_DAILY_PROFILE_PATH = Path(__file__).with_name("daily_profile.yaml")


def load_daily_profile(path: str | Path = DEFAULT_DAILY_PROFILE_PATH) -> DailyProfile:
    """Load and strictly validate a daily profile YAML file."""

    profile_path = Path(path)
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("daily_profile.yaml must contain a mapping")
    return DailyProfile.model_validate(raw)


__all__ = [
    "DAILY_SECTIONS",
    "DEFAULT_DAILY_PROFILE_PATH",
    "DailyProfile",
    "SectionProfile",
    "load_daily_profile",
]
