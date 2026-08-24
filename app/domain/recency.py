"""Deterministic Stage-A freshness policy for daily intelligence.

The AI provider must never decide whether an item is recent. Stage A is the
single admission gate: it uses the daily edition's previous-day midnight in
the project timezone, with an explicit GitHub Trending discovery exemption.
Downstream stages consume that frozen decision without applying another time
window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

STAGE_A_FRESHNESS_POLICY_VERSION = "edition_previous_day_midnight_v1"
STAGE_A_FRESHNESS_CUTOFF_MODE = "edition_previous_day_midnight"
STAGE_A_FRESHNESS_TIMEZONE = "Asia/Shanghai"
FRESHNESS_UNDATED_POLICY = "exclude"
FRESHNESS_FUTURE_POLICY = "exclude"
FRESHNESS_GITHUB_TRENDING_POLICY = "exempt"
STAGE_A_TIMEZONE = ZoneInfo(STAGE_A_FRESHNESS_TIMEZONE)


@dataclass(frozen=True)
class StageAFreshnessDecision:
    """One explainable Stage-A time-admission decision for a frozen run."""

    eligible: bool
    reason: str
    time_basis: str
    timestamp: datetime | None
    reference_time: datetime
    cutoff_at: datetime
    age_hours: float | None
    window_hours: int | None = None
    cutoff_mode: str = STAGE_A_FRESHNESS_CUTOFF_MODE
    cutoff_timezone: str | None = STAGE_A_FRESHNESS_TIMEZONE

    def metadata(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "time_basis": self.time_basis,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "reference_time": self.reference_time.isoformat(),
            "cutoff_at": self.cutoff_at.isoformat(),
            "window_hours": self.window_hours,
            "cutoff_mode": self.cutoff_mode,
            "cutoff_timezone": self.cutoff_timezone,
            "age_hours": self.age_hours,
        }


def stage_a_freshness_scope(edition_date: date | str | None = None) -> dict[str, Any]:
    """Return immutable run-scope metadata for the Stage A freshness policy."""

    scope: dict[str, Any] = {
        "freshness_policy": STAGE_A_FRESHNESS_POLICY_VERSION,
        "freshness_window_hours": None,
        "freshness_cutoff_mode": STAGE_A_FRESHNESS_CUTOFF_MODE,
        "freshness_timezone": STAGE_A_FRESHNESS_TIMEZONE,
        "freshness_undated_policy": FRESHNESS_UNDATED_POLICY,
        "freshness_future_policy": FRESHNESS_FUTURE_POLICY,
        "freshness_github_trending_policy": FRESHNESS_GITHUB_TRENDING_POLICY,
    }
    normalized_edition = _normalise_edition_date(edition_date)
    if normalized_edition is not None:
        scope["freshness_edition_date"] = normalized_edition.isoformat()
    return scope


def stage_a_cutoff_at(edition_date: date | str) -> datetime:
    """Return the previous day's midnight for a daily edition, in UTC."""

    normalized_edition = _normalise_edition_date(edition_date)
    if normalized_edition is None:
        raise ValueError("edition_date must be a valid YYYY-MM-DD date")
    previous_day = normalized_edition - timedelta(days=1)
    return datetime.combine(previous_day, time.min, tzinfo=STAGE_A_TIMEZONE).astimezone(timezone.utc)


def stage_a_time_decision(
    item: Any,
    *,
    reference_time: datetime,
    edition_date: date | str,
    source: Any | None = None,
) -> StageAFreshnessDecision:
    """Apply Stage A's inclusive previous-day-midnight admission gate."""

    reference = _as_utc(reference_time)
    if reference is None:
        raise ValueError("reference_time must be a valid datetime")
    return _decision_for_cutoff(
        item,
        source=source,
        reference=reference,
        cutoff=stage_a_cutoff_at(edition_date),
        window_hours=None,
        cutoff_mode=STAGE_A_FRESHNESS_CUTOFF_MODE,
        cutoff_timezone=STAGE_A_FRESHNESS_TIMEZONE,
    )


def _decision_for_cutoff(
    item: Any,
    *,
    source: Any | None,
    reference: datetime,
    cutoff: datetime,
    window_hours: int | None,
    cutoff_mode: str,
    cutoff_timezone: str | None,
) -> StageAFreshnessDecision:
    if _is_github_trending(source):
        return StageAFreshnessDecision(
            eligible=True,
            reason="trending_exempt",
            time_basis="captured_at_discovery",
            timestamp=_as_utc(_value(item, "captured_at")),
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=None,
            window_hours=window_hours,
            cutoff_mode=cutoff_mode,
            cutoff_timezone=cutoff_timezone,
        )
    timestamp, basis, missing_reason = _resolve_timestamp(item, source)
    if timestamp is None:
        return StageAFreshnessDecision(
            eligible=False,
            reason=missing_reason,
            time_basis=basis,
            timestamp=None,
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=None,
            window_hours=window_hours,
            cutoff_mode=cutoff_mode,
            cutoff_timezone=cutoff_timezone,
        )
    if timestamp > reference:
        return StageAFreshnessDecision(
            eligible=False,
            reason="future_timestamp",
            time_basis=basis,
            timestamp=timestamp,
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=(reference - timestamp).total_seconds() / 3600,
            window_hours=window_hours,
            cutoff_mode=cutoff_mode,
            cutoff_timezone=cutoff_timezone,
        )
    age_hours = (reference - timestamp).total_seconds() / 3600
    if timestamp < cutoff:
        return StageAFreshnessDecision(
            eligible=False,
            reason="too_old",
            time_basis=basis,
            timestamp=timestamp,
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=age_hours,
            window_hours=window_hours,
            cutoff_mode=cutoff_mode,
            cutoff_timezone=cutoff_timezone,
        )
    return StageAFreshnessDecision(
        eligible=True,
        reason="within_window",
        time_basis=basis,
        timestamp=timestamp,
        reference_time=reference,
        cutoff_at=cutoff,
        age_hours=age_hours,
        window_hours=window_hours,
        cutoff_mode=cutoff_mode,
        cutoff_timezone=cutoff_timezone,
    )


def _normalise_edition_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _resolve_timestamp(item: Any, source: Any | None) -> tuple[datetime | None, str, str]:
    transport = str(_value(source, "transport") or "").casefold()
    github_mode = str(_value(_value(source, "github"), "mode") or _value(source, "github_mode") or "").casefold()
    if transport == "github" and github_mode == "releases":
        timestamp = _as_utc(_value(item, "published_at"))
        return timestamp, "release_published_at", "missing_published_at"
    if transport == "github":
        timestamp = _as_utc(_value(item, "published_at"))
        return timestamp, "github_activity_at", "missing_published_at"
    timestamp = _as_utc(_value(item, "published_at"))
    return timestamp, "published_at", "missing_published_at"


def _is_github_trending(source: Any | None) -> bool:
    transport = str(_value(source, "transport") or "").casefold()
    github_mode = str(
        _value(_value(source, "github"), "mode") or _value(source, "github_mode") or ""
    ).casefold()
    return transport == "github" and github_mode == "trending"


def _value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# Compatibility names for callers that imported the former generic scope and
# decision types.  The rolling 72-hour decision function is intentionally not
# retained because it is no longer part of the formal pipeline.
RecentWindowDecision = StageAFreshnessDecision
recent_window_scope = stage_a_freshness_scope


__all__ = [
    "FRESHNESS_FUTURE_POLICY",
    "FRESHNESS_GITHUB_TRENDING_POLICY",
    "FRESHNESS_UNDATED_POLICY",
    "STAGE_A_FRESHNESS_CUTOFF_MODE",
    "STAGE_A_FRESHNESS_POLICY_VERSION",
    "STAGE_A_FRESHNESS_TIMEZONE",
    "RecentWindowDecision",
    "StageAFreshnessDecision",
    "recent_window_scope",
    "stage_a_freshness_scope",
    "stage_a_cutoff_at",
    "stage_a_time_decision",
]
