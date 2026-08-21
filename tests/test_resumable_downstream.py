from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest
from sqlalchemy import select

from app.ai.responses import AgentRunResult
from app.ai.skills.stage_d_selection import STAGE_D_SELECTION_SCHEMA_VERSION
from app.domain.models import FetchItem
from app.jobs.event_cluster_job import run_event_cluster_job
from app.jobs.export_job import run_intel_export_job
from app.jobs.stage_d_job import StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEventItem, IntelRunStageTask, Source
from app.storage.repository import IntelRepository


REFERENCE = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _seed(session_factory, rows: list[tuple[str, int]]) -> tuple[int, dict[str, int]]:
    with session_factory() as session:
        repo = IntelRepository(session)
        session.add(Source(
            id="downstream-source", name="Source", transport="feed", url="https://source.example/feed.xml",
            source_group="official_blog", source_role="official", primary_eligible=True,
            content_class="official_model_company",
        ))
        session.flush()
        _, build = repo.start_daily_build(edition_date="2026-08-15", reference_time=REFERENCE)
        stage = repo.ensure_stage(build.id, "analyze")
        admissions: list[dict[str, Any]] = []
        ids: dict[str, int] = {}
        for index, (title, score) in enumerate(rows, start=1):
            inserted = repo.insert_item(
                FetchItem(
                    source_id="downstream-source", external_id=f"downstream-{index}", title=title,
                    url=f"https://source.example/{index}", summary=f"{title} summary",
                    content_text=f"{title} full content", content_class="official_model_company",
                    published_at=REFERENCE, captured_at=REFERENCE,
                ),
                run_id=build.id,
            )
            assert inserted.item_id is not None
            item_id = int(inserted.item_id)
            ids[title] = item_id
            session.add(AIItemReview(
                item_id=item_id, content_class="official_model_company", topic="model_release",
                topics_json='["model_release"]', keywords_json='["release"]', entities_json='[]',
                summary_cn=f"{title} summary", b1_priority=score, status="success",
            ))
            task = repo.ensure_stage_task(stage, subject_type="item", subject_id=item_id, item_id=item_id)
            repo.complete_stage_task(task, result={"item_id": item_id, "b1_priority": score})
            admissions.append({
                "item_id": item_id, "decision": "active", "rank": index, "guarded_score": score,
                "reason_code": "fixture", "reason": "fixture", "policy_version": "fixture",
                "policy_fingerprint": "fixture",
            })
        repo.replace_candidate_admissions(build.id, admissions)
        repo.finish_stage(stage, status="succeeded")
        repo.freeze_run_scope(build.id)
        session.commit()
        return int(build.id), ids


class _StageCAgent:
    model = "test-stage-c-agent"
    transport = "responses"

    def run(self, *, function_tools, on_response, on_tool, **_kwargs):
        tools = {tool.name: tool for tool in function_tools}
        on_response(1, {"id": "response-1", "output": []})
        calls = 0

        def invoke(name: str, args: Mapping[str, Any]):
            nonlocal calls
            calls += 1
            call = {"name": name, "call_id": f"call-{calls}", "arguments": json.dumps(args)}
            output = dict(tools[name].handler(dict(args)))
            on_tool(1, call, output)
            return output

        candidates = invoke("list_candidates", {"bucket": "active", "offset": 0, "limit": 30})["items"]
        invoke("save_event_drafts", {"drafts": [
            {
                "draft_key": f"item-{item['id']}", "item_ids": [item["id"]], "title": item["title"],
                "summary_cn": item.get("summary_cn") or item["title"], "topic": item.get("topic") or "technology_insight",
                "topics": [item.get("topic") or "technology_insight"], "keywords": item.get("keywords") or [],
                "entities": item.get("entities") or [], "novelty_status": "new", "prior_event_key": None,
                "review_state": "candidate", "confidence": 90, "risk_flags": [],
            }
            for item in candidates
        ]})
        assert invoke("finalize_event_drafts", {})["ok"] is True
        return AgentRunResult("response-1", 1, calls, 0, True, {"id": "response-1", "output": []})


class _FailingStageCAgent:
    model = "test-stage-c-failure"
    transport = "responses"

    def run(self, **_kwargs):
        raise RuntimeError("stage c provider failed")


class _SelectionClient:
    model = "test-stage-d-selection"
    transport = "responses"
    max_retries = 0

    def select(self, events, *, edition, max_selected):
        return {
            "schema_version": STAGE_D_SELECTION_SCHEMA_VERSION,
            "selected": [
                {"event_id": int(event["event_id"]), "reason_code": "material_change", "reason": "测试事件适合进入日报。"}
                for event in events[:max_selected]
            ],
        }


def test_stage_d_and_export_consume_only_new_c_agent_projection(tmp_path):
    session_factory = _db()
    run_id, item_ids = _seed(session_factory, [("Current build update", 90)])
    clustered = run_event_cluster_job(session_factory=session_factory, run_id=run_id, ai_client=_StageCAgent())
    assert clustered.candidate_event_ids == clustered.current_event_ids

    selected = run_stage_d_job(
        session_factory=session_factory, run_id=run_id, profile=StageDProfile(max_selected=1), ai_client=_SelectionClient()
    )
    assert selected.selected == 1
    exported = run_intel_export_job(
        session_factory=session_factory, output_dir=tmp_path / "public", artifact_dir=tmp_path / "draft", run_id=run_id
    )
    assert exported.exported == 1
    assert "Current build update" in (tmp_path / "draft" / "intel_digest.md").read_text(encoding="utf-8")
    with session_factory() as session:
        relations = session.scalars(select(IntelEventItem)).all()
        assert {row.item_id for row in relations} == set(item_ids.values())


def test_stage_c_force_rerun_invalidates_stale_stage_d_and_export_state(tmp_path):
    session_factory = _db()
    run_id, _ = _seed(session_factory, [("Current build update", 90)])
    run_event_cluster_job(session_factory=session_factory, run_id=run_id, ai_client=_StageCAgent())
    run_stage_d_job(session_factory=session_factory, run_id=run_id, profile=StageDProfile(max_selected=1), ai_client=_SelectionClient())
    run_intel_export_job(session_factory=session_factory, output_dir=tmp_path / "public", artifact_dir=tmp_path / "draft", run_id=run_id)

    run_event_cluster_job(session_factory=session_factory, run_id=run_id, force=True, ai_client=_StageCAgent())

    with session_factory() as session:
        repo = IntelRepository(session)
        assert repo.get_stage(run_id, "stage_d") is None
        assert repo.get_stage(run_id, "export") is None


def test_failed_stage_c_force_rerun_still_invalidates_stale_downstream_state(tmp_path):
    session_factory = _db()
    run_id, _ = _seed(session_factory, [("Current build update", 90)])
    run_event_cluster_job(session_factory=session_factory, run_id=run_id, ai_client=_StageCAgent())
    run_stage_d_job(session_factory=session_factory, run_id=run_id, profile=StageDProfile(max_selected=1), ai_client=_SelectionClient())
    run_intel_export_job(session_factory=session_factory, output_dir=tmp_path / "public", artifact_dir=tmp_path / "draft", run_id=run_id)

    with pytest.raises(RuntimeError, match="stage c provider failed"):
        run_event_cluster_job(session_factory=session_factory, run_id=run_id, force=True, ai_client=_FailingStageCAgent())

    with session_factory() as session:
        repo = IntelRepository(session)
        assert repo.get_stage(run_id, "stage_d") is None
        assert repo.get_stage(run_id, "export") is None
