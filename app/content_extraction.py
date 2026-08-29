"""Source-specific article body extraction used before Stage A screening."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from app.collectors.http import HTTPClient, request_with_retry


ARTICLE_SELECTORS: dict[str, str] = {
    "google_deepmind_blog": "main .rich-text",
    "huggingface_blog": ".blog-content",
    "openrouter_blog": "article .prose-blog",
    "google_research_blog": "main",
    "nvidia_ai_blog": "article",
    "nvidia_ai_platforms_news": ".article-body",
    "google_blog_ai": "article .rich-text",
    "langchain_blog": ".blog-post-content",
    "google_developers_blog": ".rich-content",
    "ithome_ai_news": "#paragraph",
    "the_decoder_ai_news": ".entry-content",
}


def extracts_article(source_id: str) -> bool:
    return source_id == "techcrunch_ai" or source_id == "hacker_news_ai" or source_id in ARTICLE_SELECTORS


def extract_article_text(
    client: HTTPClient,
    *,
    source_id: str,
    url: str,
    external_id: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    retries: int = 0,
    timeout_seconds: float = 20.0,
) -> str | None:
    if source_id == "techcrunch_ai":
        post_id = _techcrunch_post_id(external_id, raw_payload or {})
        if post_id is None:
            return None
        response, _, error = request_with_retry(
            client,
            f"https://techcrunch.com/wp-json/wp/v2/posts/{post_id}",
            retries=retries,
            timeout_seconds=timeout_seconds,
        )
        if error is not None or response is None:
            return None
        return _techcrunch_content_text(getattr(response, "content", b""))

    response, _, error = request_with_retry(
        client,
        url,
        retries=retries,
        timeout_seconds=timeout_seconds,
        extra_headers={"Accept": "text/html,application/xhtml+xml"},
    )
    if error is not None or response is None:
        return None
    soup = BeautifulSoup(bytes(getattr(response, "content", b"")), "html.parser")
    if source_id == "hacker_news_ai":
        candidates = [*soup.select("article"), *soup.select("main")]
        node = max(candidates, key=lambda value: len(value.get_text(" ", strip=True)), default=None)
    else:
        node = soup.select_one(ARTICLE_SELECTORS[source_id])
    return _block_text(node) if node is not None else None


def _block_text(node: Any) -> str | None:
    for child in node.select("script, style, noscript"):
        child.decompose()
    blocks: list[str] = []
    for child in node.select("h1, h2, h3, p, li, blockquote, pre"):
        text = re.sub(r"\s+", " ", child.get_text(" ", strip=True)).strip()
        text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        if text:
            blocks.append(text)
    return "\n\n".join(blocks) or None


def _techcrunch_post_id(external_id: str | None, raw_payload: dict[str, Any]) -> int | None:
    for value in (external_id, raw_payload.get("id")):
        raw = parse_qs(urlsplit(str(value or "")).query).get("p", [None])[0]
        if raw and raw.isdigit():
            return int(raw)
    return None


def _techcrunch_content_text(content: bytes | str) -> str | None:
    try:
        payload = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    rendered = payload.get("content", {}).get("rendered")
    if not isinstance(rendered, str):
        return None
    node = BeautifulSoup(rendered, "html.parser")
    for child in node.select(".ad-unit, .jw-player-inline-promo, script, style, noscript"):
        child.decompose()
    return _block_text(node)


__all__ = ["ARTICLE_SELECTORS", "extract_article_text", "extracts_article"]
