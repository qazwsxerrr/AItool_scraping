from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.storage.models import EvidenceItem


class SupportsGet(Protocol):
    def get(self, url: str, *, headers: dict[str, str]): ...


@dataclass(frozen=True)
class EvidenceFetchResult:
    url: str
    final_url: str | None
    http_status: int | None
    url_validation_status: str
    fetched_title: str | None
    fetched_description: str | None
    fetched_text_preview: str | None
    raw_payload: dict[str, Any]


class EvidenceFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_bytes: int = 524288,
        user_agent: str = "AItool_scraping/0.1 (+https://example.local)",
        http_client: SupportsGet | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self._http_client = http_client

    def fetch(self, evidence: EvidenceItem) -> EvidenceFetchResult:
        parsed = urlparse(evidence.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return EvidenceFetchResult(
                url=evidence.url,
                final_url=None,
                http_status=None,
                url_validation_status="invalid",
                fetched_title=None,
                fetched_description=None,
                fetched_text_preview=None,
                raw_payload={"provider": "http", "error": "invalid_url"},
            )
        if _is_private_host(parsed.hostname):
            return EvidenceFetchResult(
                url=evidence.url,
                final_url=None,
                http_status=None,
                url_validation_status="invalid",
                fetched_title=None,
                fetched_description=None,
                fetched_text_preview=None,
                raw_payload={"provider": "http", "error": "private_host_blocked"},
            )

        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml,text/plain,*/*"}
        try:
            if self._http_client is not None:
                response = self._http_client.get(evidence.url, headers=headers)
            else:
                with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True) as client:
                    response = client.get(evidence.url, headers=headers)
        except httpx.TimeoutException:
            return _error_result(evidence.url, "timeout", "timeout")
        except Exception as exc:
            return _error_result(evidence.url, "unreachable", str(exc))

        status = int(response.status_code)
        final_url = str(getattr(response, "url", evidence.url))
        content_type = response.headers.get("content-type", "") if hasattr(response, "headers") else ""
        content = getattr(response, "content", b"")
        if isinstance(content, str):
            raw_bytes = content.encode("utf-8", errors="ignore")
        else:
            raw_bytes = bytes(content[: self.max_bytes])
        text = raw_bytes.decode(_encoding_from_content_type(content_type), errors="ignore")

        if status in {401, 403}:
            validation_status = "forbidden"
        elif status in {404, 410}:
            validation_status = "unreachable"
        elif 300 <= status < 400 or final_url != evidence.url:
            validation_status = "redirected"
        elif 200 <= status < 300:
            validation_status = "reachable"
        else:
            validation_status = "unknown"

        title = _extract_title(text)
        description = _extract_meta_description(text)
        preview = _clean_text(text)[:4000] if _is_textual(content_type) else None
        return EvidenceFetchResult(
            url=evidence.url,
            final_url=final_url,
            http_status=status,
            url_validation_status=validation_status,
            fetched_title=title,
            fetched_description=description,
            fetched_text_preview=preview,
            raw_payload={
                "provider": "http",
                "status_code": status,
                "final_url": final_url,
                "content_type": content_type,
                "bytes_read": len(raw_bytes),
            },
        )


def _error_result(url: str, status: str, error: str) -> EvidenceFetchResult:
    return EvidenceFetchResult(
        url=url,
        final_url=None,
        http_status=None,
        url_validation_status=status,
        fetched_title=None,
        fetched_description=None,
        fetched_text_preview=None,
        raw_payload={"provider": "http", "error": error},
    )


def _extract_title(text: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _clean_text(unescape(match.group(1)))[:300] or None


def _extract_meta_description(text: str) -> str | None:
    match = re.search(
        r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']+)[\"']",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return _clean_text(unescape(match.group(1)))[:500] or None


def _clean_text(text: str) -> str:
    without_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    no_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return " ".join(unescape(no_tags).split())


def _is_textual(content_type: str) -> bool:
    return not content_type or any(part in content_type.lower() for part in ("text/", "html", "json", "xml"))


def _encoding_from_content_type(content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _is_private_host(hostname: str | None) -> bool:
    if not hostname:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname in {"localhost"} or hostname.endswith(".local")
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
