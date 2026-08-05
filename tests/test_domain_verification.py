from __future__ import annotations

import httpx
import pytest

from app.domain.verification import (
    COMMUNITY_SOCIAL,
    PROJECT_TOOL,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_SKIPPED,
    STATUS_VERIFIED,
    HTTPFetchResult,
    domain_matches,
    extract_domain,
    normalize_url,
    verify_item,
)


def test_url_helpers_normalize_and_reject_unsafe_urls():
    assert normalize_url(" HTTPS://WWW.Example.com:443/docs#fragment ") == "https://www.example.com/docs"
    assert extract_domain("https://www.Example.com/docs") == "example.com"
    assert domain_matches("https://docs.example.com/a", ["example.com"])
    assert not domain_matches("https://example.com.evil.test/a", ["example.com"])
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("https://user:pass@example.com") is None


def test_official_item_fetches_one_valid_candidate_and_keeps_preview():
    calls: list[tuple[str, float | None]] = []

    def fake_fetch(url: str, *, timeout: float):
        calls.append((url, timeout))
        return HTTPFetchResult(
            status_code=200,
            url=url,
            text="<html><title>Model v2 announcement</title><p>Released today.</p></html>",
        )

    result = verify_item(
        {"url": "https://news.example.test/model-v2"},
        {"content_class": "official_model_company", "official_url": "https://vendor.example.test/v2"},
        fetcher=fake_fetch,
        timeout_seconds=3,
    )

    assert result.status == STATUS_VERIFIED
    assert result.mode == "official_direct_link"
    assert result.verification_url == "https://vendor.example.test/v2"
    assert result.source_domain == "vendor.example.test"
    assert result.http_status == 200
    assert result.title == "Model v2 announcement"
    assert result.supports_basic_fact is True
    assert calls == [("https://vendor.example.test/v2", 3)]
    assert result.to_dict()["checked_at"].endswith("+00:00")


def test_official_item_falls_back_to_source_url_but_does_not_trust_bad_ai_url():
    calls: list[str] = []

    def fake_fetch(url: str):
        calls.append(url)
        return {"status_code": 200, "text": "<title>Official page</title>"}

    result = verify_item(
        {"source_url": "https://official.example.test/announcement"},
        {"content_class": "official_model_company", "official_url": "not a url"},
        fetcher=fake_fetch,
    )

    assert result.status == STATUS_VERIFIED
    assert result.verification_url == "https://official.example.test/announcement"
    assert result.url_source == "item.source_url"
    assert "invalid_url" not in result.risk_flags
    assert calls == ["https://official.example.test/announcement"]


def test_official_item_without_url_needs_review_and_does_not_call_fetcher():
    called = False

    def fake_fetch(url: str):
        nonlocal called
        called = True
        return HTTPFetchResult(200, "ok")

    result = verify_item(
        {"title": "Model announcement"},
        {"content_class": "official_model_company", "official_url": None},
        fetcher=fake_fetch,
    )

    assert result.status == STATUS_NEEDS_REVIEW
    assert "missing_official_url" in result.risk_flags
    assert called is False


@pytest.mark.parametrize(
    ("response", "risk"),
    [
        (httpx.Response(404, text="missing"), "not_found"),
        (httpx.Response(503, text="down"), "upstream_server_error"),
    ],
)
def test_official_http_errors_are_failed(response, risk):
    result = verify_item(
        {},
        {"content_class": "official_model_company", "official_url": "https://vendor.example.test/v1"},
        fetcher=lambda _url: response,
    )

    assert result.status == STATUS_FAILED
    assert result.http_status == response.status_code
    assert risk in result.risk_flags


def test_official_timeout_is_failed_and_fetch_is_called_once():
    calls = 0

    def fake_fetch(_url):
        nonlocal calls
        calls += 1
        raise httpx.TimeoutException("slow")

    result = verify_item(
        {},
        {"content_class": "official_model_company", "official_url": "https://vendor.example.test/v1"},
        fetcher=fake_fetch,
    )

    assert result.status == STATUS_FAILED
    assert result.http_status is None
    assert result.risk_flags == ["fetch_timeout"]
    assert calls == 1


def test_cross_domain_redirect_is_not_accepted_as_verified():
    response = HTTPFetchResult(
        status_code=200,
        url="https://unrelated.example.test/login",
        text="<title>Login</title>",
    )
    result = verify_item(
        {},
        {"content_class": "official_model_company", "official_url": "https://vendor.example.test/v1"},
        fetcher=lambda _url: response,
    )
    assert result.status == STATUS_FAILED
    assert "redirected_domain" in result.risk_flags


def test_project_tool_uses_metadata_only_without_fetching_or_third_party_evidence():
    called = False

    def fake_fetch(_url):
        nonlocal called
        called = True
        return HTTPFetchResult(404)

    result = verify_item(
        {
            "url": "https://github.com/example/tool",
            "title": "Example Tool",
            "metrics": {"stars": 1200, "forks": 90, "pushed_at": "2026-08-04T00:00:00Z"},
            "archived": False,
        },
        {"content_class": PROJECT_TOOL},
        fetcher=fake_fetch,
    )

    assert result.status == STATUS_SKIPPED
    assert result.mode == "metadata_only"
    assert result.verified_metadata is True
    assert result.supports_basic_fact is True
    assert result.http_status is None
    assert called is False


def test_project_risk_flags_are_deterministic_and_do_not_reject_item():
    result = verify_item(
        {
            "url": "https://github.com/example/tool",
            "metrics": {"stars": 3, "archived": True, "fork": True, "has_readme": False},
            "license": None,
        },
        {"content_class": PROJECT_TOOL},
    )

    assert result.status == STATUS_SKIPPED
    assert result.supports_basic_fact is True
    assert {"archived_repository", "fork_repository", "missing_readme", "missing_license"}.issubset(
        result.risk_flags
    )


def test_community_item_is_discovery_only_even_when_analysis_has_url():
    result = verify_item(
        {"url": "https://social.example.test/post/1", "title": "Someone mentions a model"},
        {
            "content_class": COMMUNITY_SOCIAL,
            "official_url": "https://vendor.example.test/model",
        },
        fetcher=lambda _url: pytest.fail("community items must not be fetched"),
    )

    assert result.status == STATUS_SKIPPED
    assert result.mode == "discovery_only"
    assert result.verified is False
    assert result.supports_basic_fact is False
    assert "discovery_only" in result.risk_flags
