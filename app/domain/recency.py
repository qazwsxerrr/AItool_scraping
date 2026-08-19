"""Deterministic build-scoped freshness policy for daily intelligence.

The AI provider must never decide whether an item is recent. Every run uses
its frozen ``reference_time`` and this module's 72-hour news rule, with an
explicit GitHub Trending project-discovery exemption.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config.limits import RECENT_WINDOW_HOURS


FRESHNESS_POLICY_VERSION = "recent_window_v2"
FRESHNESS_UNDATED_POLICY = "exclude"
FRESHNESS_FUTURE_POLICY = "exclude"
FRESHNESS_GITHUB_TRENDING_POLICY = "exempt"


@dataclass(frozen=True)
class RecentWindowDecision:
    """One explainable eligibility decision for an item in a frozen run."""

    eligible: bool
    reason: str
    time_basis: str
    timestamp: datetime | None
    reference_time: datetime
    cutoff_at: datetime
    age_hours: float | None

    def metadata(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "time_basis": self.time_basis,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "reference_time": self.reference_time.isoformat(),
            "cutoff_at": self.cutoff_at.isoformat(),
            "window_hours": RECENT_WINDOW_HOURS,
            "age_hours": self.age_hours,
        }


def recent_window_scope() -> dict[str, Any]:
    """Return immutable run-scope metadata for the hard freshness policy."""

    return {
        "freshness_policy": FRESHNESS_POLICY_VERSION,
        "freshness_window_hours": RECENT_WINDOW_HOURS,
        "freshness_undated_policy": FRESHNESS_UNDATED_POLICY,
        "freshness_future_policy": FRESHNESS_FUTURE_POLICY,
        "freshness_github_trending_policy": FRESHNESS_GITHUB_TRENDING_POLICY,
    }


def recent_window_decision(
    item: Any,
    *,
    reference_time: datetime,
    source: Any | None = None,
) -> RecentWindowDecision:
    """Apply the inclusive ``reference_time - 72h`` admission gate.

    Normal feed/social items must provide ``published_at``. GitHub Trending is
    an explicitly exempt project-discovery input: it is never admitted or
    rejected by a news-time window, and its optional ``captured_at`` remains
    audit-only. No other source may turn a re-fetch timestamp into a new-news
    timestamp.
    """

    reference = _as_utc(reference_time)
    if reference is None:
        raise ValueError("reference_time must be a valid datetime")
    cutoff = reference - timedelta(hours=RECENT_WINDOW_HOURS)
    if _is_github_trending(source):
        return RecentWindowDecision(
            eligible=True,
            reason="trending_exempt",
            time_basis="captured_at_discovery",
            timestamp=_as_utc(_value(item, "captured_at")),
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=None,
        )
    timestamp, basis, missing_reason = _resolve_timestamp(item, source)
    if timestamp is None:
        return RecentWindowDecision(
            eligible=False,
            reason=missing_reason,
            time_basis=basis,
            timestamp=None,
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=None,
        )
    if timestamp > reference:
        return RecentWindowDecision(
            eligible=False,
            reason="future_timestamp",
            time_basis=basis,
            timestamp=timestamp,
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=(reference - timestamp).total_seconds() / 3600,
        )
    age_hours = (reference - timestamp).total_seconds() / 3600
    if timestamp < cutoff:
        return RecentWindowDecision(
            eligible=False,
            reason="too_old",
            time_basis=basis,
            timestamp=timestamp,
            reference_time=reference,
            cutoff_at=cutoff,
            age_hours=age_hours,
        )
    return RecentWindowDecision(
        eligible=True,
        reason="within_window",
        time_basis=basis,
        timestamp=timestamp,
        reference_time=reference,
        cutoff_at=cutoff,
        age_hours=age_hours,
    )


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


__all__ = [
    "FRESHNESS_FUTURE_POLICY",
    "FRESHNESS_GITHUB_TRENDING_POLICY",
    "FRESHNESS_POLICY_VERSION",
    "FRESHNESS_UNDATED_POLICY",
    "RecentWindowDecision",
    "recent_window_decision",
    "recent_window_scope",
]
