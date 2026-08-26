"""Canonical Stage-C event package shared with Stage D.

This is the only C→D product contract. Stage C may persist extra audit or
workbench fields on drafts and event rows, but the object handed to Stage D
must be exactly this package.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


CANDIDATE_EVENT_PACKAGE_FIELDS: tuple[str, ...] = (
    "event_id",
    "title",
    "summary_cn",
    "event_family_key",
    "facts",
    "publishability",
    "history_status",
    "topic",
    "source_groups",
    "eligibility_blockers",
    "editorial_caveats",
)

ELIGIBILITY_BLOCKER_CODES = frozenset(
    {
        "confirmed_repeat_without_material_change",
        "candidate_without_facts",
        "source_conflict_on_event_core",
    }
)

# Pipeline-only states. They stay on the event row for audit and must never
# enter the C→D package, or Stage D will treat process noise as editorial signal.
AUDIT_FLAG_CODES = frozenset(
    {
        "prior_event_outside_history_window",
        "history_match_not_found",
        "invalid_novelty_status",
        "invalid_publishability",
        "search_not_configured",
        "search_budget_exhausted",
        "needs_review",
    }
)

_HISTORY_STATUS_FROM_NOVELTY = {
    "new": "new",
    "repeat": "repeat",
    "uncertain": "uncertain",
    "updated": "meaningful_update",
    "meaningful_update": "meaningful_update",
}


def classify_event_risk_flags(flags: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split raw risk flags into blockers, editorial caveats, and audit flags."""

    blockers: list[str] = []
    caveats: list[str] = []
    audits: list[str] = []
    for flag in flags:
        if flag in ELIGIBILITY_BLOCKER_CODES:
            blockers.append(flag)
        elif flag in AUDIT_FLAG_CODES:
            audits.append(flag)
        else:
            caveats.append(flag)
    return blockers, caveats, audits


def build_candidate_event_package(event: Any) -> dict[str, Any]:
    """Project one materialized Stage-C event into the C→D package."""

    draft_metadata = _event_draft_metadata(event)
    source_groups = _json_strings(getattr(event, "source_groups_json", None))
    if not source_groups:
        source_group = str(getattr(event, "source_group", None) or "").strip()
        if source_group:
            source_groups = [source_group]
    raw_flags = _json_strings(getattr(event, "risk_flags_json", None))
    blockers, caveats, _audits = classify_event_risk_flags(raw_flags)
    for caveat in _strings(draft_metadata.get("caveats")):
        if caveat not in caveats and caveat not in AUDIT_FLAG_CODES:
            caveats.append(caveat)
    return {
        "event_id": int(event.id),
        "title": str(getattr(event, "title", None) or ""),
        "summary_cn": str(getattr(event, "summary_cn", None) or ""),
        "event_family_key": str(draft_metadata.get("event_family_key") or ""),
        "facts": _mapping_list(draft_metadata.get("facts")),
        "publishability": _publishability(draft_metadata, event),
        "history_status": _history_status(draft_metadata, event),
        "topic": getattr(event, "topic", None),
        "source_groups": source_groups,
        "eligibility_blockers": blockers,
        "editorial_caveats": caveats,
    }


def _event_draft_metadata(event: Any) -> dict[str, Any]:
    resolution = _mapping(_json_value(getattr(event, "resolution_raw_json", None), {}))
    return _mapping(resolution.get("draft_metadata"))


def _publishability(draft_metadata: Mapping[str, Any], event: Any) -> str:
    return str(draft_metadata.get("publishability") or getattr(event, "review_state", None) or "")


def _history_status(draft_metadata: Mapping[str, Any], event: Any) -> str:
    requested = str(draft_metadata.get("history_status") or "").strip()
    if requested:
        return requested
    novelty = str(getattr(event, "novelty_status", None) or "").casefold()
    return _HISTORY_STATUS_FROM_NOVELTY.get(novelty, "")


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_strings(value: Any) -> list[str]:
    return _strings(_json_value(value, []))


def _strings(value: Any) -> list[str]:
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        text = str(item).strip() if item is not None else ""
        if text and text not in result:
            result.append(text)
    return result


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    raw = _json_value(value, [])
    return [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


__all__ = [
    "AUDIT_FLAG_CODES",
    "CANDIDATE_EVENT_PACKAGE_FIELDS",
    "ELIGIBILITY_BLOCKER_CODES",
    "build_candidate_event_package",
    "classify_event_risk_flags",
]
