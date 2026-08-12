"""Deterministic scoring, quota composition and publication gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from app.config.daily_profile import DailyProfile, load_daily_profile


@dataclass(frozen=True)
class GateFailure:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class PublicationGateResult:
    publishable: bool
    failures: tuple[GateFailure, ...] = ()

    @property
    def gate_failures(self) -> list[dict[str, Any]]:
        return [failure.to_dict() for failure in self.failures]

    def to_dict(self) -> dict[str, Any]:
        return {"publishable": self.publishable, "failures": self.gate_failures}


def event_score(event: Any, *, source: Any | None = None, now: datetime | None = None) -> float:
    """Apply the frozen 30/25/20/15/10 editorial score weights."""

    values = _mapping(event)
    source_values = _mapping(source) if source is not None else values
    authority = _authority_score(source_values)
    impact = _bounded(values.get("impact_score"), values.get("impact"), values.get("score"))
    novelty = _bounded(values.get("novelty_score"), values.get("novelty"))
    readability = _bounded(values.get("readability_score"), values.get("readability"))
    support = _bounded(values.get("multi_source_support"), values.get("support_score"))
    if support == 0:
        evidence = values.get("evidence") or values.get("evidence_ids") or []
        support = min(100.0, float(len(evidence)) * 35.0) if isinstance(evidence, (list, tuple, set)) else 0.0
    return round(authority * 0.30 + impact * 0.25 + novelty * 0.20 + support * 0.15 + readability * 0.10, 4)


def primary_evidence_eligible(source: Any, document: Any | None = None, *, claim_type: str | None = None) -> bool:
    """Check source governance and direct-document requirements locally."""

    source_values = _mapping(source)
    tier = _text(source_values.get("tier")) or "p4"
    citation_policy = _text(source_values.get("citation_policy")) or "discovery_only"
    primary_eligible = bool(source_values.get("primary_eligible", False))
    group = _text(source_values.get("source_group")) or ""
    if tier not in {"p1", "p2"} or citation_policy != "primary" or not primary_eligible:
        return False
    if group in {"reddit_fixed", "reddit_search", "linux_do", "x_official", "x_social", "x_search"}:
        return False
    if group in {"github_trending", "github_release", "github_search", "producthunt"} and claim_type in {
        "release", "performance", "price", "availability",
    }:
        # Project metadata can be primary for repository facts, but not for
        # concrete external release/performance/price claims without a direct
        # document URL.
        if document is None:
            return False
    if document is None:
        return False
    document_values = _mapping(document)
    status = _text(document_values.get("status")) or ""
    url = _text(document_values.get("canonical_url") or document_values.get("source_url") or document_values.get("url"))
    http_status = document_values.get("http_status") or document_values.get("status_code")
    return status in {"fetched", "verified", "success"} and bool(url) and (
        http_status is None or 200 <= int(http_status) < 400
    )


def compose_daily_selection(
    candidates: Iterable[Any],
    profile: DailyProfile | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    event_window_hours: int | None = None,
) -> list[Any]:
    """Fill sections up to targets while respecting all deterministic caps.

    The function deliberately does not pad a section: unavailable candidates
    leave that section empty rather than promoting low-signal social noise.
    """

    policy = profile if isinstance(profile, DailyProfile) else DailyProfile.model_validate(profile or _default_profile())
    current = _as_utc(now) or datetime.now(timezone.utc)
    window = int(event_window_hours or policy.event_window_hours)
    rows = list(candidates)
    rows.sort(key=lambda value: event_score(value, now=current), reverse=True)
    selected: list[Any] = []
    source_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    aggregate_github = 0
    for row in rows:
        values = _mapping(row)
        if not _within_window(values, current, window):
            continue
        section = _text(values.get("section"))
        section_profile = policy.sections.get(section) if section else None
        if section_profile is None:
            continue
        if sum(1 for item in selected if _text(_mapping(item).get("section")) == section) >= section_profile.target:
            continue
        source_id = _text(values.get("source_id") or values.get("primary_source_id")) or "unknown"
        if source_counts.get(source_id, 0) >= policy.max_per_source:
            continue
        group = _text(values.get("source_group")) or _text(_mapping(values.get("source")).get("source_group")) or ""
        cap = policy.source_group_caps.get(group)
        if cap is not None and group_counts.get(group, 0) >= cap:
            continue
        if group.startswith("github_") and aggregate_github >= policy.github_aggregate_cap:
            continue
        selected.append(row)
        source_counts[source_id] = source_counts.get(source_id, 0) + 1
        group_counts[group] = group_counts.get(group, 0) + 1
        if group.startswith("github_"):
            aggregate_github += 1
        if len(selected) >= policy.target_events:
            break
    return selected


def evaluate_publication_gates(
    events: Iterable[Any],
    profile: DailyProfile | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    editorial_reviews: Mapping[Any, Any] | None = None,
) -> PublicationGateResult:
    """Return machine-readable publication failures; never infer a publish."""

    policy = profile if isinstance(profile, DailyProfile) else DailyProfile.model_validate(profile or _default_profile())
    rows = list(events)
    failures: list[GateFailure] = []
    if not policy.minimum_publishable_events <= len(rows) <= policy.target_events:
        failures.append(GateFailure("event_count", "selected events must be within publishable range", {"count": len(rows), "minimum": policy.minimum_publishable_events, "maximum": policy.target_events}))
    section_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    github_count = 0
    model_primary = 0
    for row in rows:
        values = _mapping(row)
        section = _text(values.get("section")) or "unknown"
        section_counts[section] = section_counts.get(section, 0) + 1
        source_id = _text(values.get("source_id") or values.get("primary_source_id")) or "unknown"
        source_counts[source_id] = source_counts.get(source_id, 0) + 1
        group = _text(values.get("source_group")) or _text(_mapping(values.get("source")).get("source_group")) or ""
        group_counts[group] = group_counts.get(group, 0) + 1
        if group.startswith("github_"):
            github_count += 1
        if section == "model_product" and primary_evidence_eligible(values.get("source") or values, values.get("document") or values.get("primary_document")):
            model_primary += 1
        if editorial_reviews is not None:
            review_key = values.get("id") or values.get("event_id")
            review = editorial_reviews.get(review_key)
            if review is None or not _review_success(review):
                failures.append(GateFailure("editorial_missing", "event lacks successful structured editorial output", {"event_id": review_key}))
        elif not _review_success(values.get("editorial_review")):
            failures.append(GateFailure("editorial_missing", "event lacks successful structured editorial output", {"event_id": values.get("id") or values.get("event_id")}))
    for source_id, count in source_counts.items():
        if count > policy.max_per_source:
            failures.append(GateFailure("source_cap", "source exceeds daily cap", {"source_id": source_id, "count": count, "cap": policy.max_per_source}))
    for group, count in group_counts.items():
        cap = policy.source_group_caps.get(group)
        if cap is not None and count > cap:
            failures.append(GateFailure("source_group_cap", "source group exceeds daily cap", {"source_group": group, "count": count, "cap": cap}))
    if github_count > policy.github_aggregate_cap:
        failures.append(GateFailure("github_aggregate_cap", "aggregate GitHub cap exceeded", {"count": github_count, "cap": policy.github_aggregate_cap}))
    model_minimum = policy.sections["model_product"].minimum_p1_primary
    if model_primary < model_minimum:
        failures.append(GateFailure("model_product_primary", "model_product lacks required P1 primary events", {"count": model_primary, "minimum": model_minimum}))
    return PublicationGateResult(publishable=not failures, failures=tuple(_dedupe_failures(failures)))


def _within_window(values: Mapping[str, Any], now: datetime, window_hours: int) -> bool:
    date = _as_utc(values.get("discovered_at") or values.get("published_at") or values.get("created_at"))
    return date is None or now - date <= timedelta(hours=window_hours)


def _review_success(value: Any) -> bool:
    if value is None:
        return False
    status = _text(_mapping(value).get("status")) or ""
    if status and status != "success":
        return False
    facts = _mapping(value).get("facts") or _mapping(value).get("facts_json")
    return bool(facts) or bool(_mapping(value).get("title"))


def _authority_score(values: Mapping[str, Any]) -> float:
    tier = _text(values.get("tier")) or "p4"
    return {"p1": 100.0, "p2": 75.0, "p3": 45.0, "p4": 20.0}.get(tier, 20.0)


def _bounded(*values: Any) -> float:
    for value in values:
        try:
            if value is not None:
                return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            continue
    return 0.0


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump())
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _default_profile() -> dict[str, Any]:
    return load_daily_profile().model_dump()


def _dedupe_failures(failures: Iterable[GateFailure]) -> list[GateFailure]:
    result: list[GateFailure] = []
    seen: set[tuple[str, str]] = set()
    for failure in failures:
        key = (failure.code, str(failure.details))
        if key not in seen:
            result.append(failure)
            seen.add(key)
    return result


__all__ = ["GateFailure", "PublicationGateResult", "compose_daily_selection", "evaluate_publication_gates", "event_score", "primary_evidence_eligible"]
