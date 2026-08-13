from datetime import datetime, timedelta, timezone

import pytest

from app.domain import (
    COMMUNITY_SOCIAL,
    OFFICIAL_MODEL_COMPANY,
    PROJECT_TOOL,
    FetchBatch,
    FetchItem,
    classify_source,
    score_item,
    selection_decision,
    should_select,
    source_spec_from_config,
)
from app.domain.models import SourceSpec


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _source(**overrides) -> SourceSpec:
    values = {
        "id": "community_feed",
        "name": "Community Feed",
        "transport": "feed",
        "url": "https://example.test/feed.xml",
        "feed": {"format": "rss", "adapter": "generic"},
        "source_group": "community",
        "source_subtype": "fixed",
        "source_role": "community",
    }
    values.update(overrides)
    if values.get("transport") == "github":
        values.pop("feed", None)
    return SourceSpec(**values)


def _item(**overrides) -> FetchItem:
    values = {
        "source_id": "source",
        "title": "An AI intelligence item",
        "url": "https://example.test/item",
        "published_at": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return FetchItem(**values)


def test_source_classification_uses_explicit_value_then_registry_metadata():
    official = _source(
        id="openai_news",
        name="OpenAI News",
        source_group="official_blog",
        source_role=None,
    )
    github = _source(
        id="github_active",
        name="GitHub Active",
        transport="github",
        url="https://api.github.com/search/repositories",
        github={"mode": "search", "query": "agent"},
        source_group="github",
        source_subtype="search_repositories",
        source_role="code_hosting",
    )

    assert classify_source(official) == OFFICIAL_MODEL_COMPANY
    assert classify_source(github) == PROJECT_TOOL
    assert classify_source(_source()) == COMMUNITY_SOCIAL
    assert classify_source(
        _source(
            id="x_account_vendor",
            source_group="x",
            source_subtype="account",
            source_role="official",
        )
    ) == COMMUNITY_SOCIAL
    assert classify_source({"id": "forced", "content_class": PROJECT_TOOL}) == PROJECT_TOOL


def test_source_spec_resolves_default_selection_policy():
    github = source_spec_from_config(
        _source(
            id="github_active",
            name="GitHub Active",
            transport="github",
            url="https://api.github.com/search/repositories",
            github={"mode": "search", "query": "agent", "pushed_days": None},
            source_group="github",
            source_subtype="search_repositories",
            source_role="code_hosting",
        )
    )
    official = source_spec_from_config(
        _source(
            id="official_news",
            name="Official News",
            source_group="official_blog",
            source_role="official",
        )
    )
    community = source_spec_from_config(_source())

    assert github.selection_policy.mode == "github_active_high_star"
    assert github.selection_policy.pushed_days == 30
    assert github.selection_policy.min_stars == 100
    assert github.selection_policy.sort_by == "stars"
    assert official.selection_policy.max_age_days == 30
    assert community.selection_policy.max_age_days == 7


def test_explicit_policy_overrides_defaults_without_losing_class_defaults():
    spec = source_spec_from_config(
        {
            "id": "custom_github",
            "name": "Custom GitHub",
            "transport": "github",
            "url": "https://api.github.com/search/repositories",
            "github": {"mode": "search", "query": "agent"},
            "content_class": PROJECT_TOOL,
            "selection_policy": {"min_stars": 500, "pushed_days": 14},
        }
    )

    assert spec.selection_policy.mode == "github_active_high_star"
    assert spec.selection_policy.min_stars == 500
    assert spec.selection_policy.pushed_days == 14
    assert spec.selection_policy.sort_by == "stars"


def test_removed_selection_policy_fields_are_rejected():
    with pytest.raises(ValueError, match="removed fields"):
        source_spec_from_config(
            {
                "id": "legacy_policy",
                "name": "Legacy policy",
                "transport": "feed",
                "url": "https://example.test/feed.xml",
                "selection_policy": {"mode": "official_recent", "verification_policy": {"mode": "metadata_only"}},
            }
        )


def test_fetch_dtos_accept_current_collector_aliases_and_transport_metadata():
    item = FetchItem(
        source_id="github_active",
        title="GitHub repo: owner/project",
        link="https://github.com/owner/project",
        raw_summary="Project summary",
        raw_content="README",
        raw_payload={
            "stargazers_count": 1200,
            "forks_count": 80,
            "pushed_at": "2026-08-04T00:00:00Z",
        },
    )
    batch = FetchBatch(
        source_id="github_active",
        items=[item],
        http_status=200,
        response_bytes=1024,
        retry_count=1,
    )

    assert item.url == "https://github.com/owner/project"
    assert item.summary == "Project summary"
    assert item.content == "README"
    assert item.metrics["stars"] == 1200
    assert item.metrics["forks"] == 80
    assert batch.items_fetched == 1
    with pytest.raises(ValueError, match="source_id or source"):
        FetchBatch(items=[])


def test_github_requires_more_than_100_stars_and_push_within_30_days():
    source = _source(
        id="github_active",
        name="GitHub Active",
        transport="github",
        url="https://api.github.com/search/repositories",
        github={"mode": "search", "query": "agent"},
        source_group="github",
        source_subtype="search_repositories",
        source_role="code_hosting",
    )
    eligible = _item(
        source_id=source.id,
        metrics={"stars": 101, "pushed_at": NOW - timedelta(days=30)},
    )
    at_star_boundary = _item(
        source_id=source.id,
        metrics={"stars": 100, "pushed_at": NOW - timedelta(days=1)},
    )
    stale = _item(
        source_id=source.id,
        metrics={"stars": 20_000, "pushed_at": NOW - timedelta(days=31)},
    )

    assert should_select(eligible, source, now=NOW) is True
    assert selection_decision(at_star_boundary, source, now=NOW).reason == "github_stars_below_threshold"
    assert selection_decision(stale, source, now=NOW).reason == "github_push_too_old"


def test_official_items_require_30_day_recency_and_release_keyword():
    source = {
        "id": "official_news",
        "transport": "feed",
        "url": "https://example.test/news.xml",
        "feed": {"format": "rss", "adapter": "generic"},
        "source_role": "official",
        "content_class": OFFICIAL_MODEL_COMPANY,
    }
    selected = _item(title="Announcing our new model release", published_at=NOW - timedelta(days=30))
    stale = _item(title="New model release", published_at=NOW - timedelta(days=31))
    no_keyword = _item(title="A note from our research team")

    decision = selection_decision(selected, source, now=NOW)
    assert decision.selected is True
    assert "model" in decision.matched_keywords
    assert selection_decision(stale, source, now=NOW).reason == "official_item_too_old"
    assert selection_decision(no_keyword, source, now=NOW).reason == "official_keyword_missing"


@pytest.mark.parametrize("title", [
    "Company version update",
    "Pricing update for the API",
    "Model upgrade announcement",
])
def test_official_change_signals_include_company_version_price_and_update(title):
    source = {
        "id": "official_news",
        "transport": "feed",
        "url": "https://example.test/news.xml",
        "feed": {"format": "rss", "adapter": "generic"},
        "source_role": "official",
        "content_class": OFFICIAL_MODEL_COMPANY,
    }
    decision = selection_decision(_item(title=title), source, now=NOW)
    assert decision.selected is True


def test_producthunt_uses_votes_and_time_with_configurable_threshold():
    source = {
        "id": "producthunt_feed",
        "name": "Product Hunt",
        "transport": "feed",
        "url": "https://www.producthunt.com/feed",
        "feed": {"format": "atom", "adapter": "producthunt"},
        "source_group": "producthunt",
        "content_class": PROJECT_TOOL,
        "selection_policy": {"min_votes": 10, "max_age_days": 14},
    }
    selected = _item(metrics={"votes": 50}, published_at=NOW - timedelta(days=14))
    low_votes = _item(metrics={"votes": 9})
    stale = _item(metrics={"votes": 500}, published_at=NOW - timedelta(days=15))

    assert should_select(selected, source, now=NOW) is True
    assert selection_decision(low_votes, source, now=NOW).reason == "producthunt_votes_below_threshold"
    assert selection_decision(stale, source, now=NOW).reason == "producthunt_item_too_old"
    assert score_item(_item(metrics={"votes": 500}), source, now=NOW) > score_item(low_votes, source, now=NOW)


def test_community_is_seven_day_signal():
    source = _source()
    recent = _item(published_at=NOW - timedelta(days=7))
    stale = _item(published_at=NOW - timedelta(days=8))

    decision = selection_decision(recent, source, now=NOW)
    assert decision.selected is True
    assert selection_decision(stale, source, now=NOW).reason == "community_item_too_old"


def test_policy_rejects_legacy_source_routing_fields():
    with pytest.raises(ValueError, match="type"):
        source_spec_from_config(
            {
                "id": "legacy",
                "name": "Legacy",
                "type": "rss",
                "url": "https://example.test/feed.xml",
            }
        )
