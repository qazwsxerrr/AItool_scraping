from __future__ import annotations

import json
from datetime import date

from app.domain.models import FetchItem, SourceSpec
from app.github.report import render_github_trending_report
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import IntelItem
from app.storage.repository import IntelRepository


def test_github_metrics_merge_preserves_daily_weekly_and_search_signals(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'github.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)

    configs = [
        SourceSpec(
            id="github_trending_daily_native",
            name="GitHub Trending Daily",
            transport="github",
            url="https://github.com/trending?since=daily",
            github={"mode": "trending", "period": "daily"},
            source_group="github",
        ),
        SourceSpec(
            id="github_trending_weekly_native",
            name="GitHub Trending Weekly",
            transport="github",
            url="https://github.com/trending?since=weekly",
            github={"mode": "trending", "period": "weekly"},
            source_group="github",
        ),
        SourceSpec(
            id="github_search_topic_llm",
            name="GitHub Search Topic LLM",
            transport="github",
            url="https://api.github.com/search/repositories",
            github={"mode": "search", "query": "topic:llm", "pushed_days": 7},
            source_group="github",
        ),
    ]
    with session_factory() as session:
        repo = IntelRepository(session)
        for config in configs:
            repo.upsert_source(config)
        session.flush()
        _, build = repo.start_daily_build(edition_date="2026-08-19")

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
            ),
            run_id=build.id,
        )
        repo.insert_item(
            FetchItem(
                source_id=configs[1].id,
                metrics={"stars": 1010, "forks": 21, "stars_since": 180, "trending_period": "weekly", "trending_rank": 5},
                **base,
            ),
            run_id=build.id,
        )
        repo.insert_item(
            FetchItem(
                source_id=configs[2].id,
                metrics={"stars": 1020, "forks": 22, "pushed_at": "2026-08-05T00:00:00Z", "search_query": "topic:llm"},
                **base,
            ),
            run_id=build.id,
        )
        session.commit()

        rows = {row.source_id: json.loads(row.metrics_json) for row in session.query(IntelItem).all()}
        assert set(rows) == {config.id for config in configs}
        assert rows["github_trending_daily_native"]["trending"]["daily"]["stars_since"] == 30
        assert rows["github_trending_weekly_native"]["trending"]["weekly"]["stars_since"] == 180
        assert rows["github_search_topic_llm"]["search_topics"] == ["llm"]


def test_github_search_and_trending_rows_remain_separate_source_observations(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'dedupe.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    trending = SourceSpec(
        id="github_trending_weekly_native",
        name="GitHub Trending Weekly",
        transport="github",
        url="https://github.com/trending?since=weekly",
        github={"mode": "trending", "period": "weekly"},
        source_group="github",
    )
    search = SourceSpec(
        id="github_search_topic_llm",
        name="GitHub Search Topic LLM",
        transport="github",
        url="https://api.github.com/search/repositories",
        github={"mode": "search", "query": "topic:llm", "pushed_days": 7},
        source_group="github",
    )
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(trending)
        repo.upsert_source(search)
        _, build = repo.start_daily_build(edition_date="2026-08-19")
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
            ),
            run_id=build.id,
        )
        second = repo.insert_item(
            FetchItem(
                source_id=search.id,
                external_id="github_repo:12345",
                metrics={"stars": 1010, "search_query": "topic:llm"},
                **base,
            ),
            run_id=build.id,
        )
        session.commit()
        rows = list(session.query(IntelItem).order_by(IntelItem.source_id).all())

    assert first.inserted is True
    assert second.inserted is True
    assert [row.source_id for row in rows] == [search.id, trending.id]
    assert json.loads(rows[1].metrics_json)["trending"]["weekly"]["stars_since"] == 50
    assert json.loads(rows[0].metrics_json)["search_topics"] == ["llm"]


def test_github_enrichment_persists_readme_and_does_not_expose_metrics_as_readme(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'enrichment.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    source = SourceSpec(
        id="github_search_topic_llm",
        name="GitHub Search Topic LLM",
        transport="github",
        url="https://api.github.com/search/repositories",
        github={"mode": "search", "query": "topic:llm", "pushed_days": 7},
        source_group="github",
    )
    with session_factory() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source)
        _, build = repo.start_daily_build(edition_date="2026-08-19")
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
            ),
            run_id=build.id,
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
