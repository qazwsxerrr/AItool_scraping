from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TAG_RE = re.compile(r"<[^>]+>")
BLOCK_TAG_RE = re.compile(r"(?i)<\s*/?\s*(br|p|div|li|tr|h[1-6]|article|section)[^>]*>")
WHITESPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "spm",
}


@dataclass(frozen=True)
class StandardizedItem:
    item_id: int | str | None
    title: str
    body_text: str | None
    url: str | None
    author: str | None
    published_at: datetime | None
    language: str
    dedupe_key: str


def standardize_item(item: object) -> StandardizedItem:
    """Standardize collector-shaped text and identity fields without a DB dependency."""
    title = clean_text(_value(item, "title")) or "(untitled)"
    body_source = _value(item, "content") or _value(item, "raw_content") or _value(item, "summary") or _value(item, "raw_summary")
    body_text = clean_text(body_source)
    url = normalize_url(_value(item, "url") or _value(item, "link"))
    author = clean_text(_value(item, "author"))
    published_at = _as_utc(_value(item, "published_at"))
    language = detect_language(" ".join(part for part in [title, body_text] if part))
    dedupe_key = build_dedupe_key(title=title, url=url)

    return StandardizedItem(
        item_id=_value(item, "id") or _value(item, "item_id"),
        title=title,
        body_text=body_text,
        url=url,
        author=author,
        published_at=published_at,
        language=language,
        dedupe_key=dedupe_key,
    )


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = BLOCK_TAG_RE.sub(" ", str(value))
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text or None


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    raw_url = html.unescape(value).strip()
    if not raw_url:
        return None

    parts = urlsplit(raw_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return raw_url

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parts.path or ""
    if path != "/":
        path = path.rstrip("/")
    elif path == "/":
        path = ""

    filtered_query = []
    for key, query_value in parse_qsl(parts.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key in TRACKING_QUERY_KEYS:
            continue
        filtered_query.append((key, query_value))
    query = urlencode(filtered_query, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def build_dedupe_key(*, title: str, url: str | None) -> str:
    if url:
        return f"url:{url}"
    normalized_title = (clean_text(title) or "").lower()
    title_hash = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()
    return f"title:{title_hash}"


def detect_language(value: str | None) -> str:
    if not value:
        return "unknown"
    if CJK_RE.search(value):
        return "zh"
    if LATIN_RE.search(value):
        return "en"
    return "unknown"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _value(item: object, name: str):
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
