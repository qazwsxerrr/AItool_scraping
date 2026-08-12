"""Deterministic prefilter and audit helpers for V3 triage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.ai.schemas import TriageResponse
from app.domain.models import SourceSpec
from app.domain.policies import source_spec_from_config


@dataclass(frozen=True)
class TriageResult:
    keep: bool
    section: str
    event_type: str
    event_hint: str
    impact_score: int
    novelty_score: int
    readability_score: int
    risk_flags: tuple[str, ...] = ()
    reason: str = ""
    confidence: int = 0
    claim_types: tuple[str, ...] = ()
    deterministic_score: float = 0.0
    status: str = "success"

    @classmethod
    def from_response(cls, response: TriageResponse, *, deterministic_score: float = 0.0) -> "TriageResult":
        return cls(
            keep=response.keep,
            section=response.section,
            event_type=response.event_type,
            event_hint=response.event_hint,
            impact_score=response.impact_score,
            novelty_score=response.novelty_score,
            readability_score=response.readability_score,
            risk_flags=tuple(response.risk_flags),
            reason=response.reason,
            confidence=response.confidence,
            claim_types=tuple(response.claim_types),
            deterministic_score=deterministic_score,
        )


def triage_item(
    item: Any,
    source: SourceSpec | Mapping[str, Any] | Any | None = None,
    *,
    now: datetime | None = None,
    window_hours: int = 72,
    response: TriageResponse | None = None,
) -> TriageResult:
    """Apply local recency/governance rules and optionally a validated AI result."""

    values = _mapping(item)
    spec = source_spec_from_config(source) if source is not None else None
    published_at = _as_utc(values.get("published_at") or values.get("discovered_at"))
    current = _as_utc(now) or datetime.now(timezone.utc)
    recent = published_at is None or current - published_at <= timedelta(hours=window_hours)
    content_class = getattr(spec, "content_class", None) or _text(values.get("content_class")) or "community_social"
    event_hint = _text(values.get("event_hint") or values.get("event_type") or values.get("title")) or "unspecified"
    section = _section_for(content_class, values)
    deterministic = deterministic_triage_score(item, source, now=current)
    if response is not None:
        result = TriageResult.from_response(response, deterministic_score=deterministic)
        flags = list(result.risk_flags)
        if not recent and "outside_window" not in flags:
            flags.append("outside_window")
        keep = result.keep and recent and _source_candidate_allowed(spec)
        if not _source_candidate_allowed(spec) and "source_not_candidate" not in flags:
            flags.append("source_not_candidate")
        return TriageResult(**{**asdict(result), "keep": keep, "risk_flags": tuple(flags)})
    flags: list[str] = []
    if not recent:
        flags.append("outside_window")
    if not _source_candidate_allowed(spec):
        flags.append("source_not_candidate")
    return TriageResult(
        keep=recent and _source_candidate_allowed(spec),
        section=section,
        event_type=_text(values.get("event_type")) or "signal",
        event_hint=event_hint,
        impact_score=round(deterministic),
        novelty_score=round(deterministic),
        readability_score=round(deterministic),
        risk_flags=tuple(flags),
        reason="deterministic_prefilter",
        confidence=100,
        deterministic_score=deterministic,
    )


def deterministic_triage_score(
    item: Any,
    source: SourceSpec | Mapping[str, Any] | Any | None = None,
    *,
    now: datetime | None = None,
) -> float:
    """Return a stable 0-100 signal from authority, recency and text depth."""

    values = _mapping(item)
    spec = source_spec_from_config(source) if source is not None else None
    tier = getattr(spec, "tier", None) or _text(values.get("tier")) or "p4"
    authority = {"p1": 100.0, "p2": 75.0, "p3": 45.0, "p4": 20.0}.get(tier, 20.0)
    published_at = _as_utc(values.get("published_at") or values.get("discovered_at"))
    current = _as_utc(now) or datetime.now(timezone.utc)
    if published_at is None:
        freshness = 0.0
    else:
        age_hours = max(0.0, (current - published_at).total_seconds() / 3600.0)
        freshness = max(0.0, 100.0 - min(100.0, age_hours / 72.0 * 100.0))
    text = " ".join(str(values.get(key) or "") for key in ("title", "summary", "content_text", "event_hint"))
    readability = min(100.0, max(0.0, len(text.strip()) / 12.0))
    return round(authority * 0.45 + freshness * 0.4 + readability * 0.15, 4)


def build_triage_request(item: Any) -> dict[str, Any]:
    values = _mapping(item)
    return {
        "item_id": values.get("item_id", values.get("id")),
        "title": _text(values.get("title")) or "(untitled)",
        "source_id": _text(values.get("source_id")) or "unknown",
        "source_url": _text(values.get("source_url") or values.get("canonical_url") or values.get("url")),
        "body_preview": (_text(values.get("content_text") or values.get("summary")) or "")[:8000],
    }


def _source_candidate_allowed(source: Any | None) -> bool:
    if source is None:
        return True
    group = _text(getattr(source, "source_group", None)) or ""
    # Discovery-only/community items remain valid *candidates* so later stages
    # can extract direct links and supplementary evidence.  The publication
    # gates, not triage, enforce that they cannot become primary evidence.
    return group not in {"reddit_search", "x_search"}


def _section_for(content_class: str, values: Mapping[str, Any]) -> str:
    explicit = _text(values.get("section"))
    if explicit in {"model_product", "industry_infrastructure", "research", "open_source_tool", "practice_opinion"}:
        return explicit
    if content_class == "project_tool":
        return "open_source_tool"
    title = f"{values.get('title') or ''} {values.get('summary') or ''}".casefold()
    if any(token in title for token in ("paper", "arxiv", "doi", "research", "benchmark")):
        return "research"
    if any(token in title for token in ("infra", "database", "gpu", "cloud", "platform", "api")):
        return "industry_infrastructure"
    if content_class == "official_model_company":
        return "model_product"
    return "practice_opinion"


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


__all__ = ["TriageResult", "build_triage_request", "deterministic_triage_score", "triage_item"]
