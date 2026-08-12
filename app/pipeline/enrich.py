"""Small, deterministic enrichment helpers for the V3 event pipeline.

The enrichment stage intentionally does not perform network I/O itself.  Jobs
may pass a bounded fetch callback; this module turns the result into a compact
document snapshot suitable for persistence and later evidence citation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app.pipeline.normalize import clean_text, normalize_url


@dataclass(frozen=True)
class EnrichedDocument:
    item_id: int | str | None
    source_id: str | None
    canonical_url: str | None
    source_url: str | None
    title: str | None
    content_excerpt: str | None
    content_text: str | None
    content_hash: str | None
    fetched_at: datetime
    http_status: int | None = None
    status: str = "fetched"
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["fetched_at"] = self.fetched_at.isoformat()
        return value


def enrich_item(
    item: Any,
    *,
    fetcher: Callable[[str], Any] | None = None,
    now: datetime | None = None,
    max_chars: int = 12_000,
) -> EnrichedDocument:
    """Create a bounded document snapshot from an item or optional fetcher.

    ``fetcher`` can return a mapping, an HTTP-like response, or plain text. A
    failed fetch is represented as ``status='fetch_failed'`` rather than
    raising, allowing a batch job to continue and persist the error state.
    """

    values = _mapping(item)
    source_id = _text(values.get("source_id"))
    item_id = values.get("item_id", values.get("id"))
    source_url = normalize_url(_text(values.get("source_url") or values.get("url") or values.get("link")))
    canonical_url = normalize_url(_text(values.get("canonical_url") or source_url))
    title = clean_text(_text(values.get("original_title") or values.get("title")))
    body = clean_text(
        _text(values.get("content_text") or values.get("content") or values.get("content_excerpt")
              or values.get("summary") or values.get("raw_content") or values.get("raw_summary"))
    )
    metadata: dict[str, Any] = {}
    http_status = _int_or_none(values.get("http_status") or values.get("status_code"))
    status = "fetched"

    if fetcher is not None and canonical_url:
        try:
            fetched = fetcher(canonical_url)
            fetched_values = _mapping(fetched)
            if isinstance(fetched, str):
                fetched_values = {"content_text": fetched}
            elif hasattr(fetched, "text") and not fetched_values:
                fetched_values = {"content_text": getattr(fetched, "text", "")}
            body = clean_text(
                _text(
                    fetched_values.get("content_text")
                    or fetched_values.get("content")
                    or fetched_values.get("text")
                    or body
                )
            )
            title = clean_text(_text(fetched_values.get("title") or title))
            http_status = _int_or_none(
                fetched_values.get("http_status")
                or fetched_values.get("status_code")
                or getattr(fetched, "status_code", None)
                or http_status
            )
            metadata = dict(fetched_values.get("metadata") or {}) if isinstance(fetched_values.get("metadata"), Mapping) else {}
            status = "fetched" if http_status is None or 200 <= http_status < 400 else "http_error"
        except Exception as exc:  # preserve per-item failure for audit
            status = "fetch_failed"
            metadata = {"error": str(exc)[:4000], "error_type": type(exc).__name__}

    bounded = body[: max(0, int(max_chars))] if body else None
    content_hash = hashlib.sha256(bounded.encode("utf-8")).hexdigest() if bounded else None
    return EnrichedDocument(
        item_id=item_id,
        source_id=source_id,
        canonical_url=canonical_url,
        source_url=source_url,
        title=title,
        content_excerpt=bounded,
        content_text=bounded,
        content_hash=content_hash,
        fetched_at=_as_utc(now) or datetime.now(timezone.utc),
        http_status=http_status,
        status=status,
        metadata=metadata,
    )


def build_document_snapshot(item: Any, **kwargs: Any) -> EnrichedDocument:
    """Compatibility alias used by jobs and tests."""

    return enrich_item(item, **kwargs)


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


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


__all__ = ["EnrichedDocument", "build_document_snapshot", "enrich_item"]
