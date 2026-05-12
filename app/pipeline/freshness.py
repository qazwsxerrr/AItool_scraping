from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

from app.storage.models import CandidateItem, EvidenceItem


def calculate_freshness_score(candidate: CandidateItem, *, now: datetime | None = None) -> int:
    reference_time = _as_utc(now or datetime.now(timezone.utc))
    timestamps = list(_content_timestamps(candidate))
    if not timestamps:
        timestamps = list(_fetch_timestamps(candidate))
    if not timestamps:
        return 0
    newest = max(timestamps)
    age_days = max(0.0, (reference_time - newest).total_seconds() / 86400)
    if age_days <= 2:
        score = 95
    elif age_days <= 7:
        score = 85
    elif age_days <= 14:
        score = 75
    elif age_days <= 30:
        score = 65
    elif age_days <= 90:
        score = 50
    elif age_days <= 180:
        score = 35
    else:
        score = 20

    claim = candidate.extracted_claim
    if claim and claim.release_signal:
        score += 5
    if claim and claim.actionable_signal:
        score += 3
    return _clamp(score)


def _content_timestamps(candidate: CandidateItem) -> Iterable[datetime]:
    item = candidate.normalized_item
    if item and item.published_at:
        yield _as_utc(item.published_at)
    raw_item = item.raw_item if item else None
    if raw_item and raw_item.published_at:
        yield _as_utc(raw_item.published_at)
    for evidence in candidate.evidence_items:
        yield from _evidence_payload_timestamps(evidence)


def _fetch_timestamps(candidate: CandidateItem) -> Iterable[datetime]:
    item = candidate.normalized_item
    raw_item = item.raw_item if item else None
    if raw_item and raw_item.fetched_at:
        yield _as_utc(raw_item.fetched_at)
    for evidence in candidate.evidence_items:
        if evidence.fetched_at:
            yield _as_utc(evidence.fetched_at)


def _evidence_payload_timestamps(evidence: EvidenceItem) -> Iterable[datetime]:
    try:
        payload = json.loads(evidence.raw_payload or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        return
    for key in [
        "pushed_at",
        "lastModified",
        "last_modified",
        "updated_at",
        "created_at",
        "published_at",
        "fetched_at",
    ]:
        parsed = _parse_datetime(payload.get(key))
        if parsed:
            yield parsed


def _parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return _as_utc(datetime.fromisoformat(normalized))
    except ValueError:
        pass
    try:
        return _as_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clamp(value: int | float) -> int:
    return max(0, min(int(round(value)), 100))
