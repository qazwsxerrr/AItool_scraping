"""Lightweight, policy-driven verification for the simplified intelligence flow.

This module deliberately has no dependency on the database or on any Job.  It
turns one item and its (untrusted) AI analysis into a small, auditable result:

* official model/company items get one direct-link check;
* project/tool items use the metadata already supplied by the source;
* community items remain discovery-only.

The HTTP boundary is injectable so callers and tests can provide a shared
client, a transport, or a tiny fake callable.  An AI-suggested URL is only a
candidate: it is syntax-checked and fetched before it can produce ``verified``.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx


OFFICIAL_MODEL_COMPANY = "official_model_company"
PROJECT_TOOL = "project_tool"
COMMUNITY_SOCIAL = "community_social"
CONTENT_CLASSES = frozenset({OFFICIAL_MODEL_COMPANY, PROJECT_TOOL, COMMUNITY_SOCIAL})

MODE_OFFICIAL = "official_direct_link"
MODE_METADATA = "metadata_only"
MODE_DISCOVERY = "discovery_only"

STATUS_VERIFIED = "verified"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_TITLE_RE = re.compile(r"<title(?:\s[^>]*)?>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class VerificationResult:
    """Result of the one lightweight verification decision.

    ``status`` is intentionally small and stable for storage adapters.  For
    project/community entries, ``status=skipped`` is expected: ``mode`` tells
    consumers whether metadata or discovery semantics were used.  The
    ``verified_metadata`` reports whether source metadata was sufficient for a
    project hotspot without introducing a second persisted status vocabulary.
    """

    status: str
    mode: str
    content_class: str
    verification_url: str | None = None
    source_domain: str | None = None
    http_status: int | None = None
    title: str | None = None
    content_preview: str | None = None
    supports_basic_fact: bool = False
    risk_flags: list[str] = field(default_factory=list)
    reason: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    url_source: str | None = None
    redirect_url: str | None = None
    raw_response: dict[str, Any] | None = None

    @property
    def verified(self) -> bool:
        return self.status == STATUS_VERIFIED

    @property
    def verified_metadata(self) -> bool:
        """Whether source metadata was accepted without an HTTP page check."""

        return self.mode == MODE_METADATA and self.supports_basic_fact

    @property
    def verification_status(self) -> str:
        return self.status

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checked_at"] = self.checked_at.isoformat()
        return data

    as_dict = to_dict


@dataclass(frozen=True)
class HTTPFetchResult:
    """Small response shape accepted by ``verify_item`` callers and fakes."""

    status_code: int
    text: str = ""
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


FetchCallable = Callable[..., Any]


def normalize_url(value: Any) -> str | None:
    """Return a canonical HTTP(S) URL, or ``None`` for unsafe/malformed input.

    Userinfo and non-HTTP schemes are rejected.  Fragments are removed because
    they are not sent to an HTTP server and would make deduplication unstable.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 4096:
        return None
    try:
        parsed = urlparse(text)
        hostname = parsed.hostname
        # Accessing ``port`` catches malformed ports such as ``:abc``.
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = hostname.lower().rstrip(".")
    # Keep IPv6 brackets in netloc, while avoiding duplicate default ports.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def is_valid_url(value: Any) -> bool:
    return normalize_url(value) is not None


def is_http_url(value: Any) -> bool:
    """Alias used by collectors that prefer an explicit scheme-oriented name."""

    return is_valid_url(value)


def extract_domain(value: Any) -> str | None:
    """Extract a lower-case host without a leading ``www.``."""

    normalized = normalize_url(value)
    if normalized is None:
        return None
    try:
        hostname = urlparse(normalized).hostname
    except ValueError:
        return None
    if not hostname:
        return None
    host = hostname.rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def domain_from_url(value: Any) -> str | None:
    return extract_domain(value)


def domain_matches(value: Any, allowed_domains: Iterable[str] | str | None) -> bool:
    """Check an exact domain or a proper subdomain against an allow-list."""

    if allowed_domains is None:
        return True
    domain = extract_domain(value)
    if not domain:
        return False
    if isinstance(allowed_domains, str):
        allowed_domains = [allowed_domains]
    for candidate in allowed_domains:
        candidate_text = str(candidate).strip().lower()
        if not candidate_text:
            continue
        # Permit callers to pass ``https://example.com`` as a convenience.
        candidate_domain = extract_domain(candidate_text) or candidate_text.split("/", 1)[0]
        candidate_domain = candidate_domain.removeprefix("www.").rstrip(".")
        if domain == candidate_domain or domain.endswith("." + candidate_domain):
            return True
    return False


def validate_url(value: Any) -> str | None:
    """Public alias returning the normalized URL rather than a boolean."""

    return normalize_url(value)


def verify_item(
    item: Any,
    analysis: Any,
    fetcher: FetchCallable | None = None,
    *,
    http_client: Any | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = 10.0,
    allowed_domains: Iterable[str] | str | None = None,
    max_preview_chars: int = 500,
) -> VerificationResult:
    """Apply the appropriate lightweight verification policy to one item.

    ``item`` and ``analysis`` may be dictionaries, dataclasses, or ordinary
    objects with matching attributes.  ``fetcher`` takes precedence over
    ``http_client`` and ``transport`` and is called at most once.  It may return
    an ``httpx.Response``, :class:`HTTPFetchResult`, a mapping, a ``(status,
    body)`` tuple, or a plain text body (the latter is treated as HTTP 200).
    """

    content_class = _content_class(item, analysis)
    if content_class == PROJECT_TOOL:
        return _verify_project(item, analysis, max_preview_chars=max_preview_chars)
    if content_class == COMMUNITY_SOCIAL:
        return _verify_community(item, analysis)
    return _verify_official(
        item,
        analysis,
        fetcher=fetcher,
        http_client=http_client,
        transport=transport,
        timeout_seconds=timeout_seconds,
        allowed_domains=allowed_domains,
        max_preview_chars=max_preview_chars,
    )


def _verify_official(
    item: Any,
    analysis: Any,
    *,
    fetcher: FetchCallable | None,
    http_client: Any | None,
    transport: httpx.BaseTransport | None,
    timeout_seconds: float,
    allowed_domains: Iterable[str] | str | None,
    max_preview_chars: int,
) -> VerificationResult:
    checked_at = datetime.now(timezone.utc)
    analysis_value = _first_value(analysis, "official_url")
    source_value = _source_url(item)
    invalid_candidate = bool(str(analysis_value or source_value or "").strip())
    url_source: str | None = None
    candidate = normalize_url(analysis_value)
    if candidate:
        url_source = "analysis.official_url"
    else:
        candidate = normalize_url(source_value)
        if candidate:
            url_source = "item.source_url"

    if candidate is None:
        flags = ["invalid_url"] if invalid_candidate else ["missing_official_url"]
        reason = "No valid official or source URL was supplied" if invalid_candidate else "An official direct link is required"
        status = STATUS_FAILED if invalid_candidate else STATUS_NEEDS_REVIEW
        return _result(
            status=status,
            mode=MODE_OFFICIAL,
            content_class=OFFICIAL_MODEL_COMPANY,
            risk_flags=flags,
            reason=reason,
            checked_at=checked_at,
            url_source=url_source,
        )

    domain = extract_domain(candidate)
    if not domain_matches(candidate, allowed_domains):
        return _result(
            status=STATUS_FAILED,
            mode=MODE_OFFICIAL,
            content_class=OFFICIAL_MODEL_COMPANY,
            verification_url=candidate,
            source_domain=domain,
            risk_flags=["domain_not_allowed"],
            reason="The direct-link domain is not in the configured allow-list",
            checked_at=checked_at,
            url_source=url_source,
        )

    try:
        response = _invoke_fetcher(
            fetcher or _make_http_fetcher(http_client=http_client, transport=transport, timeout_seconds=timeout_seconds),
            candidate,
            timeout_seconds,
        )
        snapshot = _coerce_fetch_result(response)
    except (httpx.TimeoutException, TimeoutError):
        return _result(
            status=STATUS_FAILED,
            mode=MODE_OFFICIAL,
            content_class=OFFICIAL_MODEL_COMPANY,
            verification_url=candidate,
            source_domain=domain,
            risk_flags=["fetch_timeout"],
            reason="The direct link timed out",
            checked_at=checked_at,
            url_source=url_source,
        )
    except Exception as exc:
        return _result(
            status=STATUS_FAILED,
            mode=MODE_OFFICIAL,
            content_class=OFFICIAL_MODEL_COMPANY,
            verification_url=candidate,
            source_domain=domain,
            risk_flags=["fetch_error"],
            reason=f"The direct link could not be fetched: {type(exc).__name__}",
            checked_at=checked_at,
            url_source=url_source,
        )

    final_url = normalize_url(snapshot.url) if snapshot.url else candidate
    redirect_url = final_url if final_url and final_url != candidate else None
    final_domain = extract_domain(final_url) if final_url else domain
    if final_domain and final_domain != domain:
        return _result(
            status=STATUS_FAILED,
            mode=MODE_OFFICIAL,
            content_class=OFFICIAL_MODEL_COMPANY,
            verification_url=candidate,
            source_domain=domain,
            http_status=snapshot.status_code,
            title=_extract_title(snapshot.text),
            content_preview=_preview(snapshot.text, max_preview_chars),
            supports_basic_fact=False,
            risk_flags=["redirected_domain"],
            reason="The direct link redirected to a different domain",
            checked_at=checked_at,
            url_source=url_source,
            redirect_url=redirect_url,
            raw_response=_raw_snapshot(snapshot, final_url),
        )

    title = _extract_title(snapshot.text)
    preview = _preview(snapshot.text, max_preview_chars)
    if 200 <= snapshot.status_code < 300:
        flags: list[str] = []
        if not title and not preview:
            flags.append("empty_response")
            return _result(
                status=STATUS_NEEDS_REVIEW,
                mode=MODE_OFFICIAL,
                content_class=OFFICIAL_MODEL_COMPANY,
                verification_url=candidate,
                source_domain=domain,
                http_status=snapshot.status_code,
                supports_basic_fact=False,
                risk_flags=flags,
                reason="The direct link succeeded but provided no basic page information",
                checked_at=checked_at,
                url_source=url_source,
                redirect_url=redirect_url,
                raw_response=_raw_snapshot(snapshot, final_url),
            )
        item_title = _clean_text(_first_value(item, "title"))
        if item_title and not _basic_fact_matches(item_title, title, preview):
            return _result(
                status=STATUS_NEEDS_REVIEW,
                mode=MODE_OFFICIAL,
                content_class=OFFICIAL_MODEL_COMPANY,
                verification_url=candidate,
                source_domain=domain,
                http_status=snapshot.status_code,
                title=title,
                content_preview=preview,
                supports_basic_fact=False,
                risk_flags=["basic_fact_mismatch"],
                reason="The direct link is reachable but does not expose the item's basic title/version signal",
                checked_at=checked_at,
                url_source=url_source,
                redirect_url=redirect_url,
                raw_response=_raw_snapshot(snapshot, final_url),
            )
        return _result(
            status=STATUS_VERIFIED,
            mode=MODE_OFFICIAL,
            content_class=OFFICIAL_MODEL_COMPANY,
            verification_url=candidate,
            source_domain=domain,
            http_status=snapshot.status_code,
            title=title,
            content_preview=preview,
            supports_basic_fact=True,
            risk_flags=flags,
            reason="The direct link returned a successful HTTP response",
            checked_at=checked_at,
            url_source=url_source,
            redirect_url=redirect_url,
            raw_response=_raw_snapshot(snapshot, final_url),
        )

    flags = _http_risk_flags(snapshot.status_code)
    return _result(
        status=STATUS_FAILED,
        mode=MODE_OFFICIAL,
        content_class=OFFICIAL_MODEL_COMPANY,
        verification_url=candidate,
        source_domain=domain,
        http_status=snapshot.status_code,
        title=title,
        content_preview=preview,
        supports_basic_fact=False,
        risk_flags=flags,
        reason=f"The direct link returned HTTP {snapshot.status_code}",
        checked_at=checked_at,
        url_source=url_source,
        redirect_url=redirect_url,
        raw_response=_raw_snapshot(snapshot, final_url),
    )


def _verify_project(item: Any, analysis: Any, *, max_preview_chars: int) -> VerificationResult:
    metrics = _metrics(item)
    payload = _first_value(item, "raw_payload", default={})
    if isinstance(payload, Mapping):
        # Some source adapters keep repository flags at the raw-payload level.
        # Reading them here remains deterministic and does not trigger another
        # request.
        for key in ("archived", "fork", "is_fork", "has_readme", "license", "license_name"):
            if key not in metrics and key in payload:
                metrics[key] = payload[key]
    flags: list[str] = []
    if _truthy(_first_value(item, "archived", default=metrics.get("archived"))):
        flags.append("archived_repository")
    if _truthy(_first_value(item, "fork", "is_fork", default=metrics.get("fork"))):
        flags.append("fork_repository")
    readme = _first_value(item, "readme", "has_readme", default=metrics.get("has_readme"))
    if readme is False:
        flags.append("missing_readme")
    license_value = _first_value(item, "license", "license_name", default=metrics.get("license"))
    if license_value is False or (license_value is None and _is_github_item(item)):
        flags.append("missing_license")
    # Preserve an available README/description as a display-only preview; it is
    # not treated as independent confirmation.
    body = _first_value(item, "summary", "description", "content", "content_text", "body_text", "body")
    preview = _preview(body, max_preview_chars)
    title = _clean_text(_first_value(item, "title", default=_first_value(analysis, "summary_cn")))
    metadata_present = any(
        _has_value(metrics.get(key))
        for key in (
            "stars",
            "forks",
            "watchers",
            "pushed_at",
            "published_at",
            "language",
            "license",
            "has_readme",
        )
    ) or any(
        _has_value(_first_value(item, key))
        for key in ("stars", "forks", "watchers", "pushed_at", "published_at", "license", "has_readme")
    )
    if not metadata_present:
        flags.append("metadata_incomplete")
    return _result(
        status=STATUS_SKIPPED,
        mode=MODE_METADATA,
        content_class=PROJECT_TOOL,
        verification_url=normalize_url(_source_url(item)),
        source_domain=extract_domain(_source_url(item)),
        title=title,
        content_preview=preview,
        supports_basic_fact=metadata_present,
        risk_flags=flags,
        reason="Project metadata is presented without independent confirmation",
        checked_at=datetime.now(timezone.utc),
        url_source="item.source_url" if _source_url(item) else None,
        raw_response={"method": MODE_METADATA, "metrics": metrics},
    )


def _verify_community(item: Any, analysis: Any) -> VerificationResult:
    source_url = normalize_url(_source_url(item))
    return _result(
        status=STATUS_SKIPPED,
        mode=MODE_DISCOVERY,
        content_class=COMMUNITY_SOCIAL,
        verification_url=source_url,
        source_domain=extract_domain(source_url),
        title=_clean_text(_first_value(item, "title")),
        supports_basic_fact=False,
        risk_flags=["discovery_only"],
        reason="Community content is retained as a discovery lead only",
        checked_at=datetime.now(timezone.utc),
        url_source="item.source_url" if source_url else None,
        raw_response={"method": MODE_DISCOVERY},
    )


def _result(**kwargs: Any) -> VerificationResult:
    flags = kwargs.get("risk_flags") or []
    kwargs["risk_flags"] = list(dict.fromkeys(str(flag) for flag in flags if str(flag).strip()))
    return VerificationResult(**kwargs)


def _content_class(item: Any, analysis: Any) -> str:
    value = _first_value(analysis, "content_class") or _first_value(item, "content_class", "source_content_class")
    normalized = str(value or "").strip().lower()
    if normalized not in CONTENT_CLASSES:
        raise ValueError("content_class must be one of: " + ", ".join(sorted(CONTENT_CLASSES)))
    return normalized


def _source_url(item: Any) -> str | None:
    for key in ("source_url", "url", "canonical_url", "link"):
        value = _first_value(item, key)
        if value:
            return str(value).strip()
    return None


def _metrics(item: Any) -> dict[str, Any]:
    value = _first_value(item, "metrics", "metrics_json", default={})
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _is_github_item(item: Any) -> bool:
    return (extract_domain(_source_url(item)) or "") == "github.com"


def _first_value(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            value = obj[name]
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _make_http_fetcher(*, http_client: Any | None, transport: httpx.BaseTransport | None, timeout_seconds: float) -> FetchCallable:
    if http_client is not None:
        def fetch_with_client(url: str, timeout: float = timeout_seconds) -> Any:
            get = getattr(http_client, "get", None)
            if not callable(get):
                raise TypeError("http_client must provide get()")
            try:
                return get(url, timeout=timeout, follow_redirects=True)
            except TypeError:
                return get(url, timeout=timeout)

        return fetch_with_client

    def fetch_with_httpx(url: str, timeout: float = timeout_seconds) -> Any:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            http2=True,
            trust_env=True,
            transport=transport,
            headers={"Accept": "text/html,application/json;q=0.9,*/*;q=0.1"},
        ) as client:
            return client.get(url)

    return fetch_with_httpx


def _invoke_fetcher(fetcher: FetchCallable, url: str, timeout_seconds: float) -> Any:
    """Call fakes with either ``fetch(url)`` or ``fetch(url, timeout=...)``."""

    try:
        signature = inspect.signature(fetcher)
        accepts_timeout = "timeout" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_timeout = False
    if accepts_timeout:
        return fetcher(url, timeout=timeout_seconds)
    return fetcher(url)


def _coerce_fetch_result(response: Any) -> HTTPFetchResult:
    if isinstance(response, HTTPFetchResult):
        return response
    if isinstance(response, str):
        return HTTPFetchResult(status_code=200, text=response)
    if isinstance(response, bytes):
        return HTTPFetchResult(status_code=200, text=response.decode("utf-8", errors="replace"))
    if isinstance(response, tuple) and len(response) >= 2:
        status, body = response[0], response[1]
        return HTTPFetchResult(status_code=int(status), text=_body_text(body))
    if isinstance(response, Mapping):
        status = response.get("status_code", response.get("status"))
        if status is None:
            raise ValueError("fetcher response has no status_code")
        return HTTPFetchResult(
            status_code=int(status),
            text=_body_text(response.get("text", response.get("body", response.get("content", "")))),
            url=str(response.get("url")) if response.get("url") else None,
            headers={str(k): str(v) for k, v in (response.get("headers") or {}).items()},
        )
    status = getattr(response, "status_code", None)
    if status is None:
        raise ValueError("fetcher response has no status_code")
    try:
        body = response.text
    except Exception:
        body = getattr(response, "content", "")
    try:
        response_url = response.url
    except (AttributeError, RuntimeError):
        # ``httpx.Response`` fakes are often created without a Request, in
        # which case the optional ``url`` property raises RuntimeError.
        response_url = None
    return HTTPFetchResult(
        status_code=int(status),
        text=_body_text(body),
        url=str(response_url) if response_url else None,
        headers={str(k): str(v) for k, v in (getattr(response, "headers", {}) or {}).items()},
    )


def _body_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _extract_title(body: str) -> str | None:
    text = _body_text(body)
    match = _TITLE_RE.search(text)
    if match:
        return _clean_text(unescape(_TAG_RE.sub(" ", match.group(1)))) or None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, Mapping):
        for key in ("title", "name", "model", "product"):
            value = _clean_text(parsed.get(key))
            if value:
                return value
    return None


def _preview(body: Any, max_chars: int) -> str | None:
    limit = max(1, int(max_chars))
    text = _body_text(body)
    if not text:
        return None
    # Keep the preview readable while avoiding returning scripts/styles or a
    # complete, potentially very large response body.
    text = _TAG_RE.sub(" ", text)
    text = unescape(_WHITESPACE_RE.sub(" ", text)).strip()
    if not text:
        return None
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = _WHITESPACE_RE.sub(" ", str(value)).strip()
    return text or None


def _has_value(value: Any) -> bool:
    return value is not None and value is not False and str(value).strip() not in {"", "None"}


def _basic_fact_matches(item_title: str, fetched_title: str | None, preview: str | None) -> bool:
    """Require a reachable page to expose at least one item identity signal."""

    haystack = " ".join(value for value in (fetched_title, preview) if value).casefold()
    if not haystack:
        return False
    tokens = re.findall(r"[a-z0-9][a-z0-9._-]{2,}|[\u4e00-\u9fff]{2,}", item_title.casefold())
    stop = {"the", "and", "for", "new", "official", "announcement", "announcing", "发布", "上线"}
    meaningful = [token for token in tokens if token not in stop]
    if not meaningful:
        return True
    return any(token in haystack for token in meaningful)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _http_risk_flags(status_code: int) -> list[str]:
    if status_code == 404:
        return ["not_found"]
    if status_code in {401, 403}:
        return ["access_denied"]
    if status_code == 429:
        return ["rate_limited"]
    if 500 <= status_code <= 599:
        return ["upstream_server_error"]
    return ["http_error"]


def _raw_snapshot(snapshot: HTTPFetchResult, final_url: str | None) -> dict[str, Any]:
    return {
        "status_code": snapshot.status_code,
        "url": final_url,
        "headers": dict(snapshot.headers),
    }


__all__ = [
    "COMMUNITY_SOCIAL",
    "CONTENT_CLASSES",
    "HTTPFetchResult",
    "MODE_DISCOVERY",
    "MODE_METADATA",
    "MODE_OFFICIAL",
    "OFFICIAL_MODEL_COMPANY",
    "PROJECT_TOOL",
    "STATUS_FAILED",
    "STATUS_NEEDS_REVIEW",
    "STATUS_SKIPPED",
    "STATUS_VERIFIED",
    "VerificationResult",
    "domain_from_url",
    "domain_matches",
    "extract_domain",
    "is_http_url",
    "is_valid_url",
    "normalize_url",
    "validate_url",
    "verify_item",
]
