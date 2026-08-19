from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.domain.models import FetchItem
from app.jobs.event_cluster_job import (
    canonical_event_key,
    cluster_candidates,
    github_repo_identity,
    normalize_event_title,
    run_event_cluster_job,
)
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, DailyEditionReportEntry, IntelEvent, IntelEventItem, Source
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _seed_daily_build(session_factory, *, edition_date: str, rows: list[dict[str, str]]) -> tuple[int, list[int]]:
    """Create only the current build inputs that Stage C is allowed to read."""

    with session_factory() as session:
        repo = IntelRepository(session)
        for source_id in sorted({row["source_id"] for row in rows}):
            session.add(
                Source(
                    id=source_id,
                    name=source_id,
                    transport="feed",
                    url=f"https://{source_id}.example/feed.xml",
                    source_group="official_blog",
                    content_class="official_model_company",
                )
            )
        session.flush()
        _, build = repo.start_daily_build(edition_date=edition_date, reference_time=NOW)
        item_ids: list[int] = []
        for row in rows:
            inserted = repo.insert_item(
                FetchItem(
                    source_id=row["source_id"],
                    external_id=row.get("external_id"),
                    url=row.get("url"),
                    title=row["title"],
                    summary=row.get("summary") or row["title"],
                    content_class="official_model_company",
                    published_at=NOW,
                    captured_at=NOW,
                ),
                run_id=build.id,
            )
            assert inserted.item_id is not None
            item_ids.append(int(inserted.item_id))
            session.add(
                AIItemReview(
                    item_id=int(inserted.item_id),
                    content_class="official_model_company",
                    topic="model",
                    topics_json=json.dumps(["model"]),
                    keywords_json=json.dumps(["model", "release"]),
                    entities_json="[]",
                    summary_cn=row.get("summary") or row["title"],
                    selection_score=80,
                    status="success",
                )
            )
        repo.freeze_run_scope(build.id)
        stage = repo.ensure_stage(build.id, "analyze")
        for item_id in item_ids:
            task = repo.ensure_stage_task(
                stage,
                subject_type="item",
                subject_id=item_id,
                item_id=item_id,
            )
            repo.complete_stage_task(task, result={"item_id": item_id})
        repo.finish_stage(stage, status="succeeded")
        session.commit()
        return int(build.id), item_ids


def _publish_prior_report(session_factory, *, edition_date: str, event_key: str, url: str, title: str) -> None:
    with session_factory() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(edition_date=edition_date, reference_time=NOW)
        repo.publish_daily_report(
            run_id=build.id,
            records=[
                {
                    "event_key": event_key,
                    "title": title,
                    "original_title": title,
                    "summary_cn": title,
                    "url": url,
                    "display_score": 80,
                    "topic": "model",
                    "content_class": "official_model_company",
                    "source_group": "official_blog",
                    "source_ids": ["prior-source"],
                    "source_refs": [],
                }
            ],
        )
        repo.delete_build(build.id)
        session.commit()


def test_identity_helpers_are_stable():
    assert normalize_event_title("  Model—Release  v1.0  ") == "model release v1 0"
    assert canonical_event_key({"url": "https://example.test/a/?utm_medium=x"}) == "url:https://example.test/a"
    assert canonical_event_key({"external_id": " GUID 42 "}) == "external:guid42"
    assert github_repo_identity("https://WWW.GITHUB.COM/Owner/Repo.GIT/issues/7") == "owner/repo"


def test_different_github_repositories_do_not_fuzzy_merge_identical_text():
    groups = cluster_candidates(
        [
            {
                "id": 1,
                "title": "AI platform release",
                "summary": "The project announces a new AI platform release for developers.",
                "url": "https://github.com/acme/platform-a",
                "external_id": "github_repo:acme/platform-a",
                "keywords": ["ai", "platform", "release"],
            },
            {
                "id": 2,
                "title": "AI platform release",
                "summary": "The project announces a new AI platform release for developers.",
                "url": "https://github.com/acme/platform-b",
                "external_id": "github_repo:acme/platform-b",
                "keywords": ["ai", "platform", "release"],
            },
        ]
    )

    assert [len(group) for group in groups] == [1, 1]


def test_stage_c_reads_only_current_build_stage_b_tasks_and_persists_private_event():
    session_factory = _db()
    run_id, item_ids = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {
                "source_id": "official-a",
                "external_id": "release-1",
                "url": "https://example.test/release-1",
                "title": "Model release",
            }
        ],
    )

    result = run_event_cluster_job(session_factory=session_factory, run_id=run_id, now=NOW)

    assert result.processed == 1
    assert result.events == 1
    assert result.current_event_ids == result.event_ids
    with session_factory() as session:
        event = session.scalar(select(IntelEvent))
        assert event is not None
        assert event.build_id == run_id
        assert [relation.item_id for relation in event.event_items] == item_ids


def test_stage_c_merges_duplicate_current_build_items_but_not_across_builds():
    session_factory = _db()
    run_id, _ = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {
                "source_id": "official-a",
                "external_id": "release-1",
                "url": "https://example.test/release-1",
                "title": "Model release",
            },
            {
                "source_id": "official-b",
                "external_id": "release-1-copy",
                "url": "https://example.test/release-1?utm_source=rss",
                "title": "Model release",
            },
        ],
    )

    result = run_event_cluster_job(session_factory=session_factory, run_id=run_id, now=NOW)

    assert result.events == 1
    assert result.merged == 1
    with session_factory() as session:
        assert session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id)) is not None
        assert len(session.scalars(select(IntelEventItem)).all()) == 2


def test_stage_c_uses_prior_final_report_as_repeat_history_but_creates_a_fresh_build_row():
    session_factory = _db()
    url = "https://example.test/release-1"
    event_key = f"url:{url}"
    _publish_prior_report(
        session_factory,
        edition_date="2026-08-18",
        event_key=event_key,
        url=url,
        title="Yesterday model release",
    )
    run_id, _ = _seed_daily_build(
        session_factory,
        edition_date="2026-08-19",
        rows=[
            {
                "source_id": "official-a",
                "external_id": "release-1",
                "url": url,
                "title": "Today model release",
            }
        ],
    )

    result = run_event_cluster_job(session_factory=session_factory, run_id=run_id, now=NOW)

    assert result.events == 0
    assert result.repeats == 1
    assert len(result.current_event_ids) == 1
    with session_factory() as session:
        entry = session.scalar(select(DailyEditionReportEntry))
        event = session.scalar(select(IntelEvent).where(IntelEvent.build_id == run_id))
        assert entry is not None and entry.event_key == event_key
        assert event is not None
        assert event.event_key == event_key
        assert event.novelty_status == "repeat"
