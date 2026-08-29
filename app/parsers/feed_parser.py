from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from pydantic import BaseModel, ConfigDict, Field


class ParsedFeedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    external_id: str | None = None
    title: str
    link: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    raw_summary: str | None = None
    raw_content: str | None = None
    content_depth: str = "missing"
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str


class FeedParseError(ValueError):
    pass


def parse_feed(
    content: bytes | str,
    source_id: str,
    *,
    feed_format: str | None = None,
) -> list[ParsedFeedItem]:
    """Parse an RSS/Atom document into canonical raw feed items.

    ``feed_format`` is supplied by the resolved ``SourceSpec`` so callers can
    keep the configured format visible at the boundary.  ``feedparser`` is
    deliberately still responsible for tolerant XML parsing; many real-world
    feeds advertise a slightly different MIME/version string than their
    registry entry.
    """
    if feed_format is not None and feed_format not in {"rss", "atom"}:
        raise FeedParseError(f"unsupported feed format: {feed_format}")
    parsed = feedparser.parse(content)
    entries = list(parsed.get("entries", []))
    if parsed.get("bozo") and not entries:
        raise FeedParseError(str(parsed.get("bozo_exception", "failed to parse feed")))

    items: list[ParsedFeedItem] = []
    for entry in entries:
        title = _as_text(entry.get("title")) or "(untitled)"
        link = _first_link(entry)
        external_id = _as_text(entry.get("id") or entry.get("guid") or link)
        raw_summary = _as_text(entry.get("summary") or entry.get("description"))
        raw_content = _extract_content(entry)
        content_depth = "full" if raw_content else ("summary" if raw_summary else "missing")
        author = _extract_author(entry)
        published_at = _extract_datetime(entry)
        raw_payload = _jsonable(dict(entry))
        content_hash = _content_hash(title=title, link=link, summary=raw_summary, content=raw_content)

        items.append(
            ParsedFeedItem(
                source_id=source_id,
                external_id=external_id,
                title=title,
                link=link,
                author=author,
                published_at=published_at,
                raw_summary=raw_summary,
                raw_content=raw_content,
                content_depth=content_depth,
                raw_payload=raw_payload,
                content_hash=content_hash,
            )
        )
    return items


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    # RSSHub/X descriptions are often plain text rather than HTML.  Do not
    # pass those values through BeautifulSoup.get_text(" "), which turns the
    # intentional line and paragraph breaks in a post into one long sentence.
    # Normalize whitespace within each line while retaining the source line
    # structure for the UI's ``white-space: pre-wrap`` renderer.
    if not re.search(r"<\s*/?[A-Za-z][^>]*>", value):
        text = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        if not lines:
            return None
        # Keep at most one blank line between paragraphs.  This preserves X
        # post formatting without allowing feed noise to create huge gaps.
        result: list[str] = []
        blank = False
        for line in lines:
            if line:
                result.append(line)
                blank = False
            elif not blank:
                result.append("")
                blank = True
        return "\n".join(result).strip() or None
    node = BeautifulSoup(value, "html.parser")
    for child in node.select("script, style, noscript"):
        child.decompose()
    blocks: list[str] = []
    for child in node.select("h1, h2, h3, p, li, blockquote, pre"):
        text = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        if text:
            blocks.append(text)
    if blocks:
        return "\n\n".join(blocks)
    text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
    return text or None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_link(entry: Any) -> str | None:
    direct_link = _as_text(entry.get("link"))
    if direct_link:
        return direct_link
    links = entry.get("links") or []
    for link_info in links:
        href = _as_text(link_info.get("href") if isinstance(link_info, dict) else None)
        if href:
            return href
    return None


def _extract_content(entry: Any) -> str | None:
    content_values = entry.get("content") or []
    if isinstance(content_values, list):
        for value in content_values:
            if isinstance(value, dict):
                text = _as_text(value.get("value"))
                if text:
                    return text
    return _as_text(entry.get("content_encoded"))


def _extract_author(entry: Any) -> str | None:
    author = _as_text(entry.get("author"))
    if author:
        return author
    authors = entry.get("authors") or []
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, dict):
            return _as_text(first.get("name") or first.get("email"))
    return None


def _extract_datetime(entry: Any) -> datetime | None:
    for parsed_key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed_value = entry.get(parsed_key)
        if parsed_value:
            return datetime(*parsed_value[:6], tzinfo=timezone.utc)

    for text_key in ("published", "updated", "created", "date"):
        text_value = _as_text(entry.get(text_key))
        if not text_value:
            continue
        try:
            parsed_dt = date_parser.parse(text_value)
        except (TypeError, ValueError, OverflowError):
            # A malformed date on one feed entry must not discard the whole
            # source batch.  Try the next date field (for example ``updated``)
            # and let the local recent-window policy handle an undated item.
            continue
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return parsed_dt.astimezone(timezone.utc)
    return None


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except TypeError:
        return str(value)


def _content_hash(*, title: str, link: str | None, summary: str | None, content: str | None) -> str:
    parts = [title.strip().lower(), link or "", summary or "", content or ""]
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
