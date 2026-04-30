from __future__ import annotations

import logging
import time
import subprocess

import httpx

from app.config.settings import DEFAULT_USER_AGENT
from app.config.source_registry import SourceConfig
from app.parsers.feed_parser import ParsedFeedItem, parse_feed

LOGGER = logging.getLogger(__name__)


class HTTPFeedCollector:
    """Fetch RSS/Atom/RSSHub feed content over HTTP and parse it with feedparser."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.user_agent = user_agent

    def collect(self, source: SourceConfig, limit: int | None = None) -> list[ParsedFeedItem]:
        content = self.fetch(source.url)
        items = parse_feed(content, source_id=source.id)
        if limit is not None:
            return items[:limit]
        return items

    def fetch(self, url: str) -> bytes:
        headers = {"User-Agent": self.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, headers=headers) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    return response.content
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                if _should_try_curl_fallback(url, exc):
                    fallback = _fetch_with_curl(url, self.timeout_seconds, self.user_agent)
                    if fallback:
                        return fallback
                if attempt >= self.retries:
                    break
                sleep_seconds = min(0.5 * (attempt + 1), 2.0)
                LOGGER.warning("Feed request failed, retrying in %.1fs: %s", sleep_seconds, exc)
                time.sleep(sleep_seconds)
        raise RuntimeError(f"failed to fetch feed {url}: {last_error}")


def _should_try_curl_fallback(url: str, exc: Exception) -> bool:
    """Use curl as a second transport when httpx is blocked or unstable.

    Some feeds are intermittently reachable from Windows curl but time out in
    httpx due to network/proxy/DNS path differences. Reddit can also return 403
    to httpx while accepting curl-like clients. The fallback still validates
    that the returned body looks like XML before accepting it.
    """
    if isinstance(exc, httpx.TimeoutException):
        return True
    return "403" in str(exc) or "429" in str(exc)


def _fetch_with_curl(url: str, timeout_seconds: float, user_agent: str) -> bytes | None:
    """Fallback for feeds where httpx is blocked or times out but curl works."""
    try:
        completed = subprocess.run(
            [
                "curl",
                "-L",
                "--max-time",
                str(int(timeout_seconds)),
                "-A",
                user_agent,
                "-H",
                "Accept: application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "-s",
                url,
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if completed.returncode == 0 and completed.stdout.strip().startswith((b"<?xml", b"<feed", b"<rss")):
        LOGGER.warning("Fetched %s with curl fallback after HTTP client failed", url)
        return completed.stdout
    return None
