"""Narrow ambiguity resolver used by Stage C event clustering.

This module deliberately exposes only merge/separate evidence.  It is not an
item-analysis skill and must never invent event title, summary, topic, or
other editorial fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


@dataclass(frozen=True)
class EventResolution:
    decision: str
    confidence: int
    reason: str | None = None
    raw: Any = None

    @property
    def merge(self) -> bool:
        return self.decision == "merge"

    @property
    def separate(self) -> bool:
        return self.decision == "separate"


def resolve_event_group(values: Iterable[Mapping[str, Any]], resolver: Callable[..., Any]) -> EventResolution:
    """Ask a narrow resolver for merge/separate evidence.

    Resolver adapters may accept the complete group or two values.  Any
    malformed/failed response is represented as ``unknown`` so callers can
    retain deterministic provenance without treating model text as fact.
    """

    rows = [dict(value) for value in values]
    if not rows:
        return EventResolution("unknown", 0, "empty_group")
    try:
        raw = resolver(rows)
        parsed = parse_event_resolution(raw)
        if parsed.decision != "unknown":
            return parsed
    except TypeError:
        pass
    except Exception as exc:
        return EventResolution("unknown", 0, "resolver_failed", {"error": str(exc)})

    if len(rows) < 2:
        return EventResolution("unknown", 0, "resolver_no_decision")
    decisions: list[EventResolution] = []
    for index in range(1, len(rows)):
        try:
            parsed = parse_event_resolution(resolver(rows[0], rows[index]))
        except Exception as exc:
            decisions.append(EventResolution("unknown", 0, "resolver_failed", {"error": str(exc)}))
            continue
        decisions.append(parsed)
    if decisions and all(value.merge for value in decisions):
        return EventResolution("merge", min(value.confidence for value in decisions), "pairwise_merge", decisions)
    if decisions and any(value.separate for value in decisions):
        return EventResolution("separate", max(value.confidence for value in decisions), "pairwise_separate", decisions)
    return EventResolution("unknown", 0, "resolver_no_decision", decisions)


def parse_event_resolution(raw: Any) -> EventResolution:
    data = dict(raw) if isinstance(raw, Mapping) else {}
    if isinstance(raw, str):
        data = {"decision": raw}
    if isinstance(raw, bool):
        data = {"decision": "merge" if raw else "separate"}
    value = data.get("decision") or data.get("resolution") or data.get("relation") or data.get("merge")
    if isinstance(value, bool):
        value = "merge" if value else "separate"
    decision = str(value).strip().casefold() if value is not None else "unknown"
    if decision in {"related", "merged", "same", "yes", "true"}:
        decision = "merge"
    elif decision in {"split", "unrelated", "different", "no", "false"}:
        decision = "separate"
    elif decision not in {"merge", "separate"}:
        decision = "unknown"
    try:
        confidence = max(0, min(100, int(float(data.get("confidence", data.get("score", 0))))))
    except (TypeError, ValueError, OverflowError):
        confidence = 0
    reason = data.get("reason") or data.get("evidence") or data.get("explanation")
    return EventResolution(decision, confidence, str(reason) if reason else None, raw)


__all__ = ["EventResolution", "parse_event_resolution", "resolve_event_group"]
