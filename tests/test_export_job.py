from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.ai.skills.stage_d_selection import STAGE_D_SELECTION_SCHEMA_VERSION
from app.domain.models import FetchItem
from app.jobs.export_job import _markdown, _verification_markdown_line, run_intel_export_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, Source
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 19, 8, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _exportable_build(session_factory) -> tuple[int, list[int]]:
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
        event_ids: list[int] = []
        for index in range(3):
            inserted = repo.insert_item(
                FetchItem(
                    source_id="export-source",
                    external_id=f"export-{index}",
                    url=f"https://example.test/export-{index}",
                    title=f"Stage C 标题 {index}",
                    summary=f"Stage C 摘要 {index}",
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
                    b1_priority=80,
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
                display_score=80 - index,
                novelty_status="new",
                state="candidate",
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
            event_ids.append(int(event.id))

        cluster = repo.ensure_stage(build.id, "cluster")
        cluster_task = repo.ensure_stage_task(
            cluster,
            subject_type="run",
            subject_id=build.id,
            target_run_id=build.id,
        )
        repo.complete_stage_task(
            cluster_task,
            result={
                "current_event_ids": event_ids,
                "candidate_event_ids": event_ids,
            },
        )
        repo.finish_stage(cluster, status="succeeded")

        stage_d = repo.ensure_stage(build.id, "stage_d")
        stage_d_task = repo.ensure_stage_task(
            stage_d,
            subject_type="run",
            subject_id=build.id,
            target_run_id=build.id,
        )
        selected = [
            {
                "event_id": event_ids[1],
                "reason_code": "top_impact",
                "reason": "影响范围最大。",
            },
            {
                "event_id": event_ids[0],
                "reason_code": "reader_value",
                "reason": "对目标读者最有帮助。",
            },
        ]
        repo.complete_stage_task(
            stage_d_task,
            result={
                "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
                "candidate_event_ids": event_ids,
                "selected": selected,
                "input_fingerprint": "selection-input",
                "config_fingerprint": "selection-config",
                "provider_attempts": 1,
            },
        )
        repo.finish_stage(stage_d, status="succeeded")
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id), event_ids


def test_export_uses_stage_d_order_but_stage_c_content(tmp_path):
    session_factory = _db()
    run_id, event_ids = _exportable_build(session_factory)
    artifact_dir = tmp_path / "draft-artifacts"

    result = run_intel_export_job(
        session_factory=session_factory,
        output_dir=tmp_path / "public-intel",
        artifact_dir=artifact_dir,
        run_id=run_id,
    )

    assert result.exported == 2
    assert result.jsonl_path == str(artifact_dir / "intel_items.jsonl")
    manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 7
    assert manifest["edition_date"] == "2026-08-19"
    assert manifest["selected_count"] == 2
    assert "watchlist_count" not in manifest
    assert "run_id" not in manifest

    records = [
        json.loads(line)
        for line in (artifact_dir / "intel_items.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event_id"] for record in records] == [event_ids[1], event_ids[0]]
    assert [record["display_order"] for record in records] == [1, 2]
    assert [record["title"] for record in records] == ["Stage C 标题 1", "Stage C 标题 0"]
    assert [record["summary_cn"] for record in records] == ["Stage C 摘要 1", "Stage C 摘要 0"]
    assert [record["reason_code"] for record in records] == ["top_impact", "reader_value"]
    assert "run_id" not in json.dumps(records, ensure_ascii=False)
    assert "display_title_zh" not in json.dumps(records, ensure_ascii=False)

    digest = (artifact_dir / "intel_digest.md").read_text(encoding="utf-8")
    assert digest.index("Stage C 标题 1") < digest.index("Stage C 标题 0")
    assert "Stage C 标题 2" not in digest
    assert "观察" not in digest
    assert "选稿依据：影响范围最大。" in digest
    assert "来源：[Export source](https://example.test/export-1)" in digest
    assert "来源组：" not in digest
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

def test_export_rejects_an_invalid_or_legacy_stage_d_result(tmp_path):
    session_factory = _db()
    with session_factory() as session:
        repo = IntelRepository(session)
        _, build = repo.start_daily_build(edition_date="2026-08-19", reference_time=NOW)
        stage_d = repo.ensure_stage(build.id, "stage_d")
        task = repo.ensure_stage_task(
            stage_d,
            subject_type="run",
            subject_id=build.id,
            target_run_id=build.id,
        )
        repo.complete_stage_task(task, result={"selected": 1})
        repo.finish_stage(stage_d, status="succeeded")
        session.commit()
        run_id = int(build.id)

    with pytest.raises(RuntimeError, match="unsupported schema"):
        run_intel_export_job(
            session_factory=session_factory,
            output_dir=tmp_path / "public-intel",
            artifact_dir=tmp_path / "draft-artifacts",
            run_id=run_id,
        )


def test_markdown_only_shows_verified_evidence_as_review_basis():
    line = _verification_markdown_line(
        {
            "verification_refs": [
                {
                    "url": "https://verify.example/review",
                    "title": "待复核页面",
                    "status": "needs_review",
                },
                {
                    "url": "https://verify.example/confirmed",
                    "title": "已核验页面",
                    "status": "verified",
                },
            ]
        }
    )

    assert line == "- 复核依据：[已核验页面](https://verify.example/confirmed)"


def test_markdown_omits_review_line_when_there_is_no_verified_evidence():
    record = {
        "record_type": "intel_event",
        "event_id": 1,
        "display_order": 1,
        "display_score": 80,
        "title": "仅有搜索线索的事件",
        "topic": "model_release",
        "content_class": "official_model_company",
        "summary_cn": "摘要",
        "reason": "保留理由",
        "risk_flags": [],
        "source_refs": [],
        "verification_refs": [
            {
                "url": "https://verify.example/recorded",
                "title": "搜索结果",
                "status": "recorded",
            }
        ],
    }

    assert _verification_markdown_line(record) is None
    markdown = _markdown([record])
    assert "核验来源：无" not in markdown
    assert "待人工核验链接" not in markdown
    assert "复核依据" not in markdown


def test_markdown_shows_primary_source_and_secondary_sources_as_related_links():
    markdown = _markdown(
        [
            {
                "record_type": "intel_event",
                "event_id": 1,
                "display_order": 1,
                "display_score": 90,
                "title": "同一事件的多来源报道",
                "topic": "model_release",
                "content_class": "official_model_company",
                "summary_cn": "摘要",
                "reason": "选稿依据",
                "risk_flags": [],
                "url": "https://primary.example/article",
                "source_refs": [
                    {
                        "source_id": "primary",
                        "source_name": "主来源",
                        "source_url": "https://primary.example/article",
                        "title": "主来源文章",
                        "is_primary": True,
                    },
                    {
                        "source_id": "secondary",
                        "source_name": "补充来源",
                        "source_url": "https://secondary.example/article",
                        "title": "补充报道",
                        "is_primary": False,
                    },
                ],
                "verification_refs": [],
            }
        ]
    )

    assert "来源：[主来源](https://primary.example/article)" in markdown
    assert "来源组：" not in markdown
    assert "相关链接：[补充来源](https://secondary.example/article)" in markdown
    assert "相关链接：[主来源]" not in markdown
