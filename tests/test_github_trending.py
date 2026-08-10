from __future__ import annotations

import json
from datetime import date

from app.config.source_registry import SourceConfig
from app.domain.models import FetchItem, SourceSpec
from app.domain.policies import should_select
from app.github.report import render_github_trending_report
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem
from app.storage.repository import IntelRepository


def test_github_trending_policy_requires_period_star_signal():
    source = SourceSpec(
        id="github_trending_weekly_native",
        name="GitHub Trending Weekly",
        transport="github",
        url="https://github.com/trending?since=weekly",
        github={"mode": "trending", "period": "weekly"},
        source_subtype="trending_weekly",
        content_class="project_tool",
        selection_policy={"mode": "github_trending"},
    )
    selected = FetchItem(
        source_id=source.id,
        title="GitHub repo: owner/tool",
        url="https://github.com/owner/tool",
        metrics={"stars": 1200, "stars_since": 250},
    )
    missing = selected.model_copy(update={"metrics": {"stars": 1200}})

    assert should_select(selected, source)
    assert not should_select(missing, source)


def test_github_metrics_merge_preserves_daily_weekly_and_search_signals(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'github.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    configs = [
        SourceConfig(
            id="github_trending_daily_native",
            name="GitHub Trending Daily",
            transport="github",
            url="https://github.com/trending?since=daily",
            github={"mode": "trending", "period": "daily"},
            source_group="github",
            source_subtype="trending_daily",
            content_class="project_tool",
            selection_policy={"mode": "github_trending"},
        ),
        SourceConfig(
            id="github_trending_weekly_native",
            name="GitHub Trending Weekly",
            transport="github",
            url="https://github.com/trending?since=weekly",
            github={"mode": "trending", "period": "weekly"},
            source_group="github",
            source_subtype="trending_weekly",
            content_class="project_tool",
            selection_policy={"mode": "github_trending"},
        ),
        SourceConfig(
            id="github_search_topic_llm",
            name="GitHub Search Topic LLM",
            transport="github",
            url="https://api.github.com/search/repositories",
            github={"mode": "search", "query": "topic:llm", "pushed_days": 7},
            source_group="github",
            source_subtype="search_repositories",
            content_class="project_tool",
            selection_policy={"mode": "github_active_high_star", "pushed_days": 7, "min_stars": 100},
        ),
    ]
    with session_factory() as session:
        repo = IntelRepository(session)
        for config in configs:
            repo.upsert_source(config)
        session.flush()

        base = {
            "title": "GitHub repo: owner/tool",
            "url": "https://github.com/owner/tool",
            "canonical_url": "https://github.com/owner/tool",
            "external_id": "github_repo:owner/tool",
            "content_class": "project_tool",
            "summary": "AI tool",
            "raw_payload": {"github_item_type": "repository", "full_name": "owner/tool"},
        }
        repo.insert_item(
            FetchItem(
                source_id=configs[0].id,
                metrics={"stars": 1000, "forks": 20, "stars_since": 30, "trending_period": "daily", "trending_rank": 2},
                **base,
            )
        )
        repo.insert_item(
            FetchItem(
                source_id=configs[1].id,
                metrics={"stars": 1010, "forks": 21, "stars_since": 180, "trending_period": "weekly", "trending_rank": 5},
                **base,
            )
        )
        repo.insert_item(
            FetchItem(
                source_id=configs[2].id,
                metrics={"stars": 1020, "forks": 22, "pushed_at": "2026-08-05T00:00:00Z", "search_query": "topic:llm"},
                **base,
            )
        )
        session.commit()

        row = session.query(IntelItem).one()
        metrics = json.loads(row.metrics_json)
        assert metrics["trending"]["daily"]["stars_since"] == 30
        assert metrics["trending"]["weekly"]["stars_since"] == 180
        assert metrics["search_topics"] == ["llm"]
        assert set(metrics["discovery_sources"]) == {
            "github_trending_daily_native",
            "github_trending_weekly_native",
            "github_search_topic_llm",
        }
        assert metrics["stars_since"] == 180
        assert metrics["trending_period"] == "weekly"


def test_github_search_and_trending_rows_dedupe_by_canonical_repository_url(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'dedupe.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    trending = SourceConfig(
        id="github_trending_weekly_native",
        name="GitHub Trending Weekly",
        transport="github",
        url="https://github.com/trending?since=weekly",
        github={"mode": "trending", "period": "weekly"},
        source_group="github",
        source_subtype="trending_weekly",
        content_class="project_tool",
        selection_policy={"mode": "github_trending"},
    )
    search = SourceConfig(
        id="github_search_topic_llm",
        name="GitHub Search Topic LLM",
        transport="github",
        url="https://api.github.com/search/repositories",
        github={"mode": "search", "query": "topic:llm", "pushed_days": 7},
        source_group="github",
        source_subtype="search_repositories",
        content_class="project_tool",
        selection_policy={"mode": "github_active_high_star", "pushed_days": 7, "min_stars": 100},
    )
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(trending)
        repo.upsert_source(search)
        base = {
            "title": "GitHub repo: owner/tool",
            "url": "https://github.com/owner/tool",
            "canonical_url": "https://github.com/owner/tool",
            "content_class": "project_tool",
            "raw_payload": {"github_item_type": "repository", "full_name": "owner/tool"},
        }
        first = repo.insert_item(
            FetchItem(
                source_id=trending.id,
                external_id="github_repo:owner/tool",
                metrics={"stars": 1000, "stars_since": 50, "trending_period": "weekly"},
                **base,
            )
        )
        second = repo.insert_item(
            FetchItem(
                source_id=search.id,
                external_id="github_repo:12345",
                metrics={"stars": 1010, "search_query": "topic:llm"},
                **base,
            )
        )
        session.commit()
        row = session.query(IntelItem).one()

    assert first.inserted is True
    assert second.inserted is False
    assert json.loads(row.metrics_json)["trending"]["weekly"]["stars_since"] == 50
    assert json.loads(row.metrics_json)["search_topics"] == ["llm"]


def test_github_enrichment_persists_readme_and_does_not_expose_metrics_as_readme(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'enrichment.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    source = SourceConfig(
        id="github_search_topic_llm",
        name="GitHub Search Topic LLM",
        transport="github",
        url="https://api.github.com/search/repositories",
        github={"mode": "search", "query": "topic:llm", "pushed_days": 7},
        source_group="github",
        source_subtype="search_repositories",
        content_class="project_tool",
        selection_policy={"mode": "github_active_high_star", "pushed_days": 7, "min_stars": 100},
    )
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source)
        inserted = repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="github_repo:owner/tool",
                title="GitHub repo: owner/tool",
                url="https://github.com/owner/tool",
                content='{"stars": 1200}',
                metrics={"stars": 1200, "forks": 10},
                raw_payload={"github_item_type": "repository", "full_name": "owner/tool"},
                content_class="project_tool",
            )
        )
        session.flush()
        repo.save_github_enrichment(
            inserted.item_id,
            {
                "metadata": {},
                "readme_checked": True,
                "readme_present": False,
                "readme_text": None,
                "errors": ["readme:permanent_http_error"],
            },
        )
        session.commit()

        row = session.get(IntelItem, inserted.item_id)
        assert row is not None
        assert row.content_text is None

        repo.save_github_enrichment(
            inserted.item_id,
            {
                "metadata": {
                    "full_name": "owner/tool",
                    "description": "A reusable AI tool",
                    "stargazers_count": 1210,
                    "forks_count": 12,
                    "topics": ["llm", "workflow"],
                },
                "readme_checked": True,
                "readme_present": True,
                "readme_text": "# Tool\nBuild reusable AI workflows.",
                "errors": [],
            },
        )
        session.commit()

        row = session.get(IntelItem, inserted.item_id)
        assert row is not None
        assert row.summary == "A reusable AI tool"
        assert row.content_text.startswith("# Tool")
        metrics = json.loads(row.metrics_json)
        assert metrics["stars"] == 1210
        assert metrics["forks"] == 12
        assert metrics["readme_chars"] > 0


def test_github_report_uses_trending_period_labels_without_fake_week_delta():
    records = [
        {
            "content_class": "project_tool",
            "source_transport": "github",
            "source_id": "github_trending_weekly_native",
            "external_id": "github_repo:owner/tool",
            "title": "GitHub repo: owner/tool",
            "url": "https://github.com/owner/tool",
            "summary": "AI tool",
            "metrics": {
                "stars": 1200,
                "forks": 80,
                "language": "Python",
                "trending": {"weekly": {"rank": 1, "stars_since": 250}},
            },
            "raw_payload": {"github_item_type": "repository", "full_name": "owner/tool"},
        }
    ]

    report = render_github_trending_report(records, report_date=date(2026, 8, 5))

    assert "本周新增 Star" in report
    assert "本周新增 Star（GitHub Trending）：250" in report
    assert "上周增长量" not in report
