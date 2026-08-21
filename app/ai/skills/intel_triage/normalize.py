"""Deterministic text and URL normalization for triage inputs."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:  # BeautifulSoup is already a project dependency, but keep a safe fallback.
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - only used in unusually minimal installs
    BeautifulSoup = None  # type: ignore[assignment,misc]


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HSPACE_RE = re.compile(r"[ \t\f\v]+")
_NEWLINE_RE = re.compile(r"\n[ \t]*\n[ \t]*\n+")
_TAG_RE = re.compile(r"<[^>]*>")
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "spm",
}


def normalize_text(value: Any, *, preserve_newlines: bool = True) -> str:
    """Normalize plain text while retaining meaningful paragraph boundaries.

    The function is intentionally idempotent and never performs semantic
    rewriting.  It removes control characters, decodes entities, normalizes
    Unicode width, and bounds whitespace so provider prompts stay compact.
    """

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    if re.search(r"<\s*/?[A-Za-z][^>]*>", text):
        # Text callers occasionally pass an RSS HTML fragment.  Normalize it
        # here as a convenience; normalize_html uses the one-way plain-text
        # helper below and therefore cannot recurse back into this branch.
        return normalize_html(text)
    return _normalize_plain_text(text, preserve_newlines=preserve_newlines)


def _normalize_plain_text(text: str, *, preserve_newlines: bool = True) -> str:
    """Normalize text after HTML has already been removed."""

    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text).replace("\u200b", "").replace("\ufeff", "")
    text = _CONTROL_RE.sub("", text)
    if not preserve_newlines:
        return _HSPACE_RE.sub(" ", text.replace("\n", " ")).strip()

    lines = [_HSPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    # Keep one empty line between paragraphs but drop leading/trailing blanks.
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines)
    text = _NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def normalize_html(value: Any) -> str:
    """Convert an HTML fragment/document into deterministic readable text."""

    if value is None:
        return ""
    raw = str(value)
    if not raw.strip():
        return ""
    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup.find_all(("script", "style", "noscript", "template", "svg")):
            node.decompose()
        for node in soup.find_all(("br",)):
            node.replace_with("\n")
        for node in soup.find_all(("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "blockquote", "pre")):
            node.insert_before("\n")
            node.insert_after("\n")
        return _normalize_plain_text(soup.get_text(" ", strip=False))

    # Dependency-light fallback for environments that intentionally omit bs4.
    text = re.sub(r"(?is)<\s*(script|style|noscript|template|svg)\b.*?</\s*\1\s*>", " ", raw)
    text = re.sub(r"(?i)<\s*(br|p|div|li|tr|h[1-6]|article|section|blockquote|pre)\b[^>]*>", "\n", text)
    text = _TAG_RE.sub(" ", text)
    return _normalize_plain_text(text)


def normalize_url(value: Any) -> str | None:
    """Canonicalize a URL without inventing a URL for malformed input."""

    if value is None:
        return None
    raw = html.unescape(str(value)).strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return raw
    scheme = parts.scheme.casefold()
    netloc = parts.netloc.casefold()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]
    path = parts.path or ""
    if path == "/":
        path = ""
    else:
        path = path.rstrip("/")
    query_items = []
    for key, query_value in parse_qsl(parts.query, keep_blank_values=True):
        if key.casefold().startswith("utm_") or key.casefold() in _TRACKING_QUERY_KEYS:
            continue
        query_items.append((key, query_value))
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


__all__ = ["normalize_html", "normalize_text", "normalize_url"]
