from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.models import SourceSpec, content_class_for_source


@pytest.mark.parametrize(
    ("source_group", "transport", "expected"),
    [
        ("official_blog", "feed", "official_model_company"),
        ("official_research", "feed", "official_model_company"),
        ("x_official", "rsshub", "official_model_company"),
        ("tech_media", "feed", "news_media"),
        ("github_release", "github", "project_tool"),
        ("github_search", "github", "project_tool"),
        ("producthunt", "feed", "project_tool"),
        ("hacker_news", "feed", "community_social"),
        ("x_social", "rsshub", "community_social"),
    ],
)
def test_content_class_is_derived_from_source_group(source_group, transport, expected):
    assert content_class_for_source(source_group, transport) == expected


def test_github_transport_defaults_to_project_tool():
    assert content_class_for_source("custom_github", "github") == "project_tool"


def test_source_spec_ignores_explicit_content_class_and_uses_group_mapping():
    source = SourceSpec(
        id="official_source",
        name="Official source",
        transport="feed",
        url="https://example.test/feed.xml",
        source_group="official_blog",
        content_class="community_social",
    )

    assert source.content_class == "official_model_company"


@pytest.mark.parametrize(
    "removed_field",
    [
        "tier",
        "topic_scopes",
        "source_subtype",
        "source_role",
        "primary_eligible",
        "quality_weight",
        "spam_risk",
        "selection_policy",
    ],
)
def test_removed_source_classification_fields_are_rejected(removed_field):
    values = {
        "id": "source",
        "name": "Source",
        "transport": "feed",
        "url": "https://example.test/feed.xml",
        removed_field: "legacy",
    }

    with pytest.raises(ValidationError):
        SourceSpec.model_validate(values)


@pytest.mark.parametrize("legacy_field", ["type", "parser_type", "collector_type"])
def test_source_spec_rejects_legacy_routing_fields(legacy_field):
    with pytest.raises(ValidationError):
        SourceSpec.model_validate(
            {
                "id": "legacy",
                "name": "Legacy",
                "transport": "feed",
                "url": "https://example.test/feed.xml",
                legacy_field: "rss",
            }
        )
