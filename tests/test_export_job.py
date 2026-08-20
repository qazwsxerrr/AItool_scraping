from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.domain.models import FetchItem
from app.jobs.export_job import run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEventStageDSnapshot, IntelItem, Source
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _exportable_build(session_factory, *, selected_count: int = 1, include_watchlist: bool = True) -> int:
    with session_factory() as session:
        repo = IntelRepository(session)
        session.add(
            Source(
                id="export-source",
                name="Export source",
                transport="feed",
                url="https://example.test/feed.xml",
                source_group="official_blog",
                content_class="official_model_company",
            )
        )
        session.flush()
        _, build = repo.start_daily_build(edition_date="2026-08-19", reference_time=NOW)
        cluster = repo.ensure_stage(build.id, "cluster")
        current_event_ids: list[int] = []
        snapshots: list[IntelEventStageDSnapshot] = []
        for index in range(selected_count + int(include_watchlist)):
            inserted = repo.insert_item(
                FetchItem(
                    source_id="export-source",
                    external_id=f"export-{index}",
                    url=f"https://example.test/export-{index}",
                    title=f"Export event {index}",
                    summary=f"Summary {index}",
                    content_class="official_model_company",
                    published_at=NOW,
                    captured_at=NOW,
                ),
                run_id=build.id,
            )
            assert inserted.item_id is not None
            item = session.get(IntelItem, inserted.item_id)
            assert item is not None
            session.add(
                AIItemReview(
                    item_id=item.id,
                    content_class="official_model_company",
                    topic="model_release",
                    topics_json='["model_release"]',
                    keywords_json='["release"]',
                    summary_cn=item.summary,
                    selection_score=80,
                    status="success",
                )
            )
            event = repo.upsert_event(
                run_id=build.id,
                event_key=f"url:https://example.test/export-{index}",
                canonical_url=item.canonical_url,
                title=item.title,
                summary_cn=item.summary,
                topic="model_release",
                topics=["model_release"],
                keywords=["release"],
                content_class=item.content_class,
                source_group="official_blog",
                source_ids=[item.source_id],
                source_groups=["official_blog"],
                display_score=80,
                novelty_status="new",
                primary_item_id=item.id,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            repo.upsert_event_item(
                event.id,
                item.id,
                source_id=item.source_id,
                source_group="official_blog",
                is_primary=True,
            )
            current_event_ids.append(event.id)
            selected = index < selected_count
            snapshots.append(
                IntelEventStageDSnapshot(
                    run_id=build.id,
                    event_id=event.id,
                    display_order=index + 1,
                    display_score=80,
                    selected=selected,
                    topic="model_release",
                    content_class="official_model_company",
                    metadata_json=json.dumps(
                        {
                            "editorial_tier": "selected" if selected else "watchlist",
                            "watchlist_order": 1 if not selected else None,
                            "display_title_zh": f"导出展示标题 {index}",
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        session.add_all(snapshots)
        task = repo.ensure_stage_task(
            cluster,
            subject_type="run",
            subject_id=build.id,
            target_run_id=build.id,
        )
        repo.complete_stage_task(task, result={"current_event_ids": current_event_ids})
        repo.finish_stage(cluster, status="succeeded")
        stage_d = repo.ensure_stage(build.id, "stage_d")
        stage_d_task = repo.ensure_stage_task(
            stage_d,
            subject_type="run",
            subject_id=build.id,
            target_run_id=build.id,
        )
        repo.complete_stage_task(stage_d_task, result={"selected": selected_count})
        repo.finish_stage(stage_d, status="succeeded")
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id)


def test_export_writes_private_artifacts_from_the_current_build_only(tmp_path):
    session_factory = _db()
    run_id = _exportable_build(session_factory)
    artifact_dir = tmp_path / "draft-artifacts"

    result = run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "public-intel",
        artifact_dir=artifact_dir,
        run_id=run_id,
    )

    assert result.exported == 1
    assert result.jsonl_path == str(artifact_dir / "intel_items.jsonl")
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["edition_date"] == "2026-08-19"
    assert manifest["selected_count"] == 1
    assert manifest["watchlist_count"] == 1
    assert "run_id" not in manifest
    record = json.loads((artifact_dir / "intel_items.jsonl").read_text(encoding="utf-8"))
    assert record["event_key"] == "url:https://example.test/export-0"
    assert record["title"] == "导出展示标题 0"
    assert "run_id" not in json.dumps(record, ensure_ascii=False)
    assert "snapshot_key" not in json.dumps(record, ensure_ascii=False)
    digest = (artifact_dir / "intel_digest.md").read_text(encoding="utf-8")
    assert "导出展示标题 1" in digest
    assert not (tmp_path / "public-intel" / "daily" / "2026-08-19").exists()


def test_export_rejects_a_build_without_a_completed_stage_d(tmp_path):
    session_factory = _db()
    with session_factory() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(edition_date="2026-08-19", reference_time=NOW)
        session.commit()
        run_id = int(build.id)

    with pytest.raises(RuntimeError, match="stage_d_incomplete"):
        run_intel_export_job(
            session_factory=session_factory,
            output_dir=tmp_path / "public-intel",
            artifact_dir=tmp_path / "draft-artifacts",
            run_id=run_id,
        )
