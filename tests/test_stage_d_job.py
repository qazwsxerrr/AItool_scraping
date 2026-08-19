from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ai.skills.stage_d_editorial.client import StageDEditorialClient
from app.ai.skills.stage_d_editorial.prompts import STAGE_D_JSON_SCHEMA, build_stage_d_provider_payload
from app.ai.skills.stage_d_editorial import strict_parse_stage_d
from app.jobs.stage_d_job import StageDProfile, _call_editorial_provider, _fallback_decisions, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEvent, IntelEventStageDSnapshot, IntelItem, IntelRun, IntelRunStage, IntelRunStageTask, Source
from app.storage.repository import IntelRepository
from app.storage.run_snapshot_summary import build_run_snapshot_summary


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _event(
    session_factory,
    *,
    title: str,
    summary: str,
    score: int = 80,
    topic: str = "model",
    community: bool = False,
    arxiv: bool = False,
    source_group: str | None = None,
    content_class: str | None = None,
    entities: list[dict[str, str]] | None = None,
) -> int:
    with session_factory() as session:
        resolved_source_group = source_group or ("reddit_fixed" if community else "official_blog")
        resolved_content_class = content_class or ("community_social" if community else "official_model_company")
        source = Source(
            id=f"source-{title[:8]}",
            name="test source",
            transport="feed",
            url="https://example.test/feed",
            source_group=resolved_source_group,
            content_class=resolved_content_class,
        )
        item = IntelItem(
            source=source,
            title=title,
            summary=summary,
            canonical_url="https://arxiv.org/abs/123" if arxiv else f"https://example.test/{title.replace(' ', '-')}",
            content_class=source.content_class,
            content_hash=(title.encode().hex() * 8)[:64].ljust(64, "0"),
            status="candidate",
            selection_score=score,
            captured_at=datetime.now(timezone.utc),
        )
        review = AIItemReview(
            item=item,
            content_class=source.content_class,
            topic=topic,
            topics_json=json.dumps([topic]),
            summary_cn=summary,
            selection_score=score,
            risk_flags_json='["source:social_only"]' if community else "[]",
            status="success",
        )
        session.add_all([source, item, review])
        session.flush()
        event = IntelRepository(session).upsert_event(
            event_key=f"url:{item.canonical_url}",
            canonical_url=item.canonical_url,
            title=title,
            summary_cn=summary,
            topic=topic,
            topics=[topic],
            entities=entities or [],
            content_class=source.content_class,
            source_group=source.source_group,
            source_ids=[source.id],
            source_groups=[source.source_group],
            display_score=score,
            primary_item_id=item.id,
            first_seen_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        IntelRepository(session).upsert_event_item(
            event.id,
            item.id,
            source_id=source.id,
            source_group=source.source_group,
            is_primary=True,
        )
        session.commit()
        return int(event.id)


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def select_events(self, events, *, edition, total_max):
        self.calls.append((events, edition, total_max))
        return self.payload(events)


def _selected(event_id: int, *, order: int = 1, family: str = "story_01", title: str = "测试模型发布新能力", score: int = 90):
    return {
        "event_id": event_id,
        "decision": "selected",
        "display_order": order,
        "editorial_score": score,
        "story_family_id": family,
        "family_position": order,
        "display_title_zh": title,
        "title_supporting_fields": ["title", "summary_cn"],
        "reason_codes": ["high_reader_value"],
        "editorial_reason": "信息变化明确，适合本期展示。",
        "confidence": 90,
    }


def _omitted(event_id: int, *, family: str = "story_01"):
    return {
        "event_id": event_id,
        "decision": "omitted",
        "display_order": None,
        "editorial_score": 20,
        "story_family_id": family,
        "family_position": None,
        "display_title_zh": None,
        "title_supporting_fields": [],
        "reason_codes": ["story_redundant"],
        "editorial_reason": "未提供独立的信息增量。",
        "confidence": 90,
    }


def _fallback_candidate(
    event_id: int,
    score: float,
    *,
    source_group: str,
    content_class: str,
    topic: str = "model",
    entity: str | None = None,
    story: str | None = None,
    evidence: str = "trusted_or_first_party_supported",
):
    event = SimpleNamespace(
        id=event_id,
        display_score=score,
        title=f"事件{event_id}标题",
        summary_cn=f"事件{event_id}摘要足够长，包含明确变化。",
        entities=[{"name": entity}] if entity else [],
        source_groups_json=json.dumps([source_group]),
        content_class=content_class,
        topic=topic,
    )
    return {
        "event": event,
        "source_group": source_group,
        "source_groups": [source_group],
        "content_class": content_class,
        "topic": topic,
        "source_evidence_level": evidence,
        "story_family_id": story,
    }


def test_stage_d_allows_community_signal_and_persists_forced_label():
    session_factory = _db()
    event_id = _event(session_factory, title="Community model signal", summary="社区称某模型将在近期发布，信息仍待核实。", community=True)
    client = _Client(lambda _events: {"schema_version": "stage_d_editorial_v1", "decisions": [_selected(event_id, title="社区称模型近期将发布")]})

    result = run_stage_d_job(session_factory=session_factory, event_ids=[event_id], ai_client=client)

    assert result.selected == 1
    with session_factory() as session:
        snapshot = session.query(IntelEventStageDSnapshot).one()
        metadata = json.loads(snapshot.metadata_json)
        assert snapshot.selected is True
        assert metadata["source_presentation"] == "community_signal_pending_verification"
        assert metadata["display_title_zh"] == "社区称模型近期将发布"


def test_stage_d_marks_independently_corroborated_community_signals():
    session_factory = _db()
    event_id = _event(
        session_factory,
        title="Community model signal",
        summary="社区称某模型将在近期发布，信息仍待核实。",
        community=True,
    )
    with session_factory() as session:
        source = Source(
            id="community-second",
            name="second community source",
            transport="feed",
            url="https://example.test/community-second",
            source_group="linux_do",
            content_class="community_social",
        )
        item = IntelItem(
            source=source,
            title="Second community model signal",
            summary="另一独立社区也称某模型将在近期发布，信息仍待核实。",
            canonical_url="https://example.test/community-second/item",
            content_class="community_social",
            content_hash="c" * 64,
            status="candidate",
            selection_score=75,
            captured_at=datetime.now(timezone.utc),
        )
        review = AIItemReview(
            item=item,
            content_class="community_social",
            topic="model",
            topics_json='["model"]',
            summary_cn=item.summary,
            selection_score=75,
            risk_flags_json='["source:social_only"]',
            status="success",
        )
        session.add_all([source, item, review])
        session.flush()
        IntelRepository(session).upsert_event_item(
            event_id,
            item.id,
            source_id=source.id,
            source_group=source.source_group,
        )
        session.commit()

    client = _Client(lambda _events: {"schema_version": "stage_d_editorial_v1", "decisions": [_selected(event_id, title="社区称模型近期将发布")]})
    result = run_stage_d_job(session_factory=session_factory, event_ids=[event_id], ai_client=client)

    assert result.selected == 1
    with session_factory() as session:
        metadata = json.loads(session.query(IntelEventStageDSnapshot).one().metadata_json)
        assert metadata["source_evidence_level"] == "multi_community_signal"
        assert metadata["source_presentation"] == "multi_community_signal_pending_verification"
        assert set(client.calls[0][0][0]["source_ids"]) == {"source-Communit", "community-second"}


def test_stage_d_keeps_only_paper_hard_gate_locally():
    session_factory = _db()
    event_id = _event(session_factory, title="Unsupported paper", summary="论文摘要", topic="paper", arxiv=True)
    client = _Client(lambda _events: pytest.fail("paper-gated event must not be sent to the provider"))

    result = run_stage_d_job(session_factory=session_factory, event_ids=[event_id], ai_client=client)

    assert result.paper_gated == 1
    assert result.selected == 0
    assert client.calls == []
    with session_factory() as session:
        snapshot = session.query(IntelEventStageDSnapshot).one()
        assert snapshot.reason == "paper_gate:arxiv_only"


def test_stage_d_fallback_has_no_topic_source_or_repeat_quota():
    session_factory = _db()
    first = _event(session_factory, title="First same source model", summary="第一条模型更新摘要足够长。", score=90)
    second = _event(session_factory, title="Second same source model", summary="第二条模型更新摘要同样足够长。", score=80)

    result = run_stage_d_job(
        session_factory=session_factory,
        event_ids=[first, second],
        ai_client=None,
        profile=StageDProfile(total_max=30),
    )

    assert result.used_fallback is True
    assert result.selected == 2
    with session_factory() as session:
        snapshots = session.query(IntelEventStageDSnapshot).order_by(IntelEventStageDSnapshot.display_order).all()
        assert [row.display_order for row in snapshots if row.selected] == [1, 2]


def test_stage_d_fallback_soft_diversity_does_not_fill_all_github_project_pool():
    candidates = [
        _fallback_candidate(index, 95 - index % 5, source_group="github_search", content_class="project_tool", entity=f"repo-{index}")
        for index in range(1, 31)
    ] + [
        _fallback_candidate(100 + index, 88 - index % 3, source_group="official_blog", content_class="official_model_company", topic="product", entity=f"model-{index}")
        for index in range(10)
    ]
    decisions = _fallback_decisions(candidates, total_max=30)
    selected = [decision for decision in decisions.values() if decision["decision"] == "selected"]
    selected_groups = {candidates[decision["event_id"] - 1]["source_group"] if decision["event_id"] <= 30 else "official_blog" for decision in selected}
    github_selected = sum(1 for decision in selected if decision["event_id"] <= 30)
    assert github_selected < len(selected)
    assert selected_groups == {"github_search", "official_blog"}


def test_stage_d_fallback_penalizes_repeated_entity_story_but_keeps_high_base_candidate():
    rows = [
        _fallback_candidate(1, 100, source_group="github_search", content_class="project_tool", entity="Alpha", story="alpha"),
        _fallback_candidate(2, 99, source_group="github_search", content_class="project_tool", entity="Alpha", story="alpha"),
        _fallback_candidate(3, 60, source_group="official_blog", content_class="official_model_company", entity="Beta", story="beta"),
    ]
    decisions = _fallback_decisions(rows, total_max=2)
    selected = sorted(
        (decision for decision in decisions.values() if decision["decision"] == "selected"),
        key=lambda decision: decision["display_order"],
    )
    assert [decision["event_id"] for decision in selected] == [1, 2]
    repeated = decisions[2]
    components = repeated["fallback_score_components"]
    assert components["same_primary_entity_penalty"] > 0
    assert components["same_story_penalty"] > 0
    assert "fallback_repeat_primary_entity" in repeated["reason_codes"]
    assert "fallback_repeat_story" in repeated["reason_codes"]


def test_stage_d_fallback_ties_are_stable_and_metadata_is_auditable():
    rows = [
        _fallback_candidate(2, 90, source_group="github_search", content_class="project_tool", entity="same"),
        _fallback_candidate(1, 90, source_group="github_search", content_class="project_tool", entity="same"),
        _fallback_candidate(3, 90, source_group="official_blog", content_class="official_model_company", entity="other"),
    ]
    first = _fallback_decisions(rows, total_max=2)
    second = _fallback_decisions(rows, total_max=2)
    assert first == second
    selected = sorted((row for row in first.values() if row["decision"] == "selected"), key=lambda row: row["display_order"])
    assert [row["event_id"] for row in selected] == [3, 1]
    assert all("fallback_rank" in row and "fallback_score_components" in row for row in first.values())

    session_factory = _db()
    event_ids = [
        _event(session_factory, title="Alpha fallback", summary="Alpha fallback 摘要足够长。", score=90, source_group="github_search", content_class="project_tool", entities=[{"name": "Alpha"}]),
        _event(session_factory, title="Beta fallback", summary="Beta fallback 摘要足够长。", score=89, source_group="official_blog", content_class="official_model_company", entities=[{"name": "Beta"}]),
    ]
    run_stage_d_job(session_factory=session_factory, event_ids=event_ids, ai_client=None, profile=StageDProfile(total_max=1))
    with session_factory() as session:
        snapshots = session.query(IntelEventStageDSnapshot).order_by(IntelEventStageDSnapshot.event_id).all()
        for snapshot in snapshots:
            metadata = json.loads(snapshot.metadata_json)
            assert metadata["fallback_rank"] >= 1
            assert set(("base", "bonus", "same_source_group_penalty", "same_content_class_penalty", "same_topic_penalty", "same_primary_entity_penalty", "same_story_penalty", "adjusted")) <= set(metadata["fallback_score_components"])


def test_stage_d_fallback_handles_missing_content_and_source_metadata():
    event = SimpleNamespace(
        id=1,
        display_score=90,
        title="缺失来源元数据事件",
        summary_cn="缺失来源元数据但标题摘要有效。",
        topic="model",
    )
    decisions = _fallback_decisions(
        [{"event": event, "source_evidence_level": "trusted_or_first_party_supported"}],
        total_max=1,
    )
    assert decisions[1]["decision"] == "selected"
    assert decisions[1]["fallback_score_components"]["bonus"] == 0


def test_stage_d_schema_rejects_invalid_title_even_when_family_is_overfilled():
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [
            _selected(1, order=1, family="same", title="重磅模型发布新能力"),
            _selected(2, order=2, family="same", title="另一个足够长度的标题"),
            _selected(3, order=3, family="same", title="第三个足够长度的标题"),
        ],
    }
    with pytest.raises(ValueError):
        strict_parse_stage_d(response, event_ids=[1, 2, 3])


def test_stage_d_local_story_family_guard_omits_third_selected():
    third = _selected(3, order=3, family="same")
    third["family_position"] = 2
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [
            _selected(1, order=1, family="same"),
            _selected(2, order=2, family="same"),
            third,
        ],
    }
    parsed = strict_parse_stage_d(response, event_ids=[1, 2, 3])
    selected = [row for row in parsed.decisions if row.decision == "selected"]
    omitted = next(row for row in parsed.decisions if row.event_id == 3)
    assert [row.event_id for row in selected] == [1, 2]
    assert omitted.decision == "omitted"
    assert omitted.reason_codes == ["high_reader_value", "local_story_family_limit"]
    assert omitted.display_title_zh is None
    assert omitted.title_supporting_fields == []
    assert omitted.editorial_reason == "本地 guard：同一 story_family_id 最多保留两条。"


def test_stage_d_local_guard_reserves_space_for_its_reason_code():
    third = _selected(3, order=3, family="same")
    third["family_position"] = 2
    third["reason_codes"] = [f"provider_reason_{index}" for index in range(12)]
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [
            _selected(1, order=1, family="same"),
            _selected(2, order=2, family="same"),
            third,
        ],
    }

    parsed = strict_parse_stage_d(response, event_ids=[1, 2, 3])
    omitted = next(row for row in parsed.decisions if row.event_id == 3)

    assert len(omitted.reason_codes) == 12
    assert omitted.reason_codes[-1] == "local_story_family_limit"


def test_stage_d_local_total_guard_omits_after_total_max():
    third = _selected(3, order=3, family="three")
    third["family_position"] = 1
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [
            _selected(1, order=1, family="one"),
            _selected(2, order=2, family="two"),
            third,
        ],
    }
    parsed = strict_parse_stage_d(response, event_ids=[1, 2, 3], total_max=2)
    assert [row.event_id for row in parsed.decisions if row.decision == "selected"] == [1, 2]
    omitted = next(row for row in parsed.decisions if row.event_id == 3)
    assert omitted.reason_codes == ["high_reader_value", "local_total_limit"]
    assert omitted.editorial_reason == "本地 guard：日报最多保留 2 条 selected。"


def test_stage_d_local_guard_reorders_display_and_family_positions_deterministically():
    first = _selected(10, order=2, family="family-a")
    first["family_position"] = 2
    second = _selected(11, order=1, family="family-a")
    second["family_position"] = 1
    third = _selected(12, order=2, family="family-b")
    third["family_position"] = 1
    parsed = strict_parse_stage_d(
        {"schema_version": "stage_d_editorial_v1", "decisions": [first, second, third]},
        event_ids=[10, 11, 12],
    )
    selected = [row for row in parsed.decisions if row.decision == "selected"]
    assert [row.event_id for row in selected] == [11, 10, 12]
    assert [row.display_order for row in selected] == [1, 2, 3]
    assert [row.family_position for row in selected] == [1, 2, 1]


def test_stage_d_schema_does_not_coerce_structural_types():
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [_selected("1")],
    }

    with pytest.raises(ValueError):
        strict_parse_stage_d(response, event_ids=[1])


def test_stage_d_schema_rejects_title_with_input_external_number_or_certainty():
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [
            _selected(1, title="社区称模型已确认将在2027年发布"),
        ],
    }

    with pytest.raises(ValueError):
        strict_parse_stage_d(
            response,
            event_ids=[1],
            events=[
                {
                    "event_id": 1,
                    "title": "Community discussion about a possible model release",
                    "summary_cn": "社区称该模型可能在近期发布，尚待核实。",
                    "source_evidence_level": "single_community_signal",
                }
            ],
        )


def test_stage_d_schema_omits_recently_displayed_event_without_material_update():
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [_selected(1, title="模型发布新增开发者功能")],
    }
    event = {
        "event_id": 1,
        "title": "Model adds developer features",
        "summary_cn": "模型发布新增开发者功能。",
        "source_evidence_level": "trusted_or_first_party_supported",
        "recent_daily_history": {"appeared_recently": True, "prior_editions": ["2026-08-17"]},
    }

    parsed = strict_parse_stage_d(response, event_ids=[1], events=[event])
    assert parsed.decisions[0].decision == "omitted"
    assert "recent_repeat_without_material_update" in parsed.decisions[0].reason_codes

    response["decisions"][0]["reason_codes"].append("material_update")
    parsed = strict_parse_stage_d(response, event_ids=[1], events=[event])
    assert parsed.decisions[0].decision == "selected"


def test_stage_d_schema_deduplicates_provider_event_ids_deterministically():
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [
            _selected(1, order=1, score=82, title="同一事件的较低分版本"),
            _selected(1, order=2, score=91, title="同一事件的较高分版本"),
            {**_selected(2, order=3, score=80, title="另一个独立事件更新"), "family_position": 1},
        ],
    }

    parsed = strict_parse_stage_d(response, event_ids=[1, 2])
    selected = [row for row in parsed.decisions if row.decision == "selected"]
    assert [row.event_id for row in selected] == [1, 2]
    assert selected[0].editorial_score == 91
    assert "provider_duplicate_event_id" in selected[0].reason_codes


def test_stage_d_qwen_story_family_can_select_two_and_omit_a_repost():
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [
            _selected(1, order=1, family="qwen_day0", title="Qwen 发布 Day-0 合作计划"),
            _selected(2, order=2, family="qwen_day0", title="Qwen 合作带来独立开发者更新"),
            _omitted(3, family="qwen_day0"),
        ],
    }
    parsed = strict_parse_stage_d(response, event_ids=[1, 2, 3])
    assert [row.event_id for row in parsed.decisions if row.decision == "selected"] == [1, 2]


def test_stage_d_accepts_fewer_than_thirty_without_filling():
    response = {
        "schema_version": "stage_d_editorial_v1",
        "decisions": [_selected(1), _omitted(2)],
    }
    parsed = strict_parse_stage_d(response, event_ids=[1, 2], total_max=30)
    assert len([row for row in parsed.decisions if row.decision == "selected"]) == 1


def test_stage_d_schema_version_has_string_type_and_selected_only_materializes_omissions():
    assert STAGE_D_JSON_SCHEMA["properties"]["schema_version"] == {
        "type": "string",
        "const": "stage_d_editorial_v1",
    }
    parsed = strict_parse_stage_d(
        {"schema_version": "stage_d_editorial_v1", "decisions": [_selected(1)]},
        event_ids=[1, 2],
    )
    assert {row.event_id for row in parsed.decisions} == {1, 2}
    omitted = next(row for row in parsed.decisions if row.event_id == 2)
    assert omitted.decision == "omitted"
    assert omitted.reason_codes == ["provider_omitted"]


def test_stage_d_provider_schema_is_selected_only_with_bounded_titles():
    properties = STAGE_D_JSON_SCHEMA["properties"]["decisions"]["items"]["properties"]
    assert properties["decision"] == {"type": "string", "const": "selected"}
    assert properties["display_order"] == {"type": "integer", "minimum": 1}
    assert properties["family_position"] == {"type": "integer", "minimum": 1, "maximum": 2}
    assert properties["display_title_zh"] == {"type": "string", "minLength": 8, "maxLength": 60}
    assert properties["title_supporting_fields"]["minItems"] == 1


def test_stage_d_accepts_37_to_60_character_titles_without_truncation():
    title = "模型发布新能力并为开发者提供完整工作流支持与多种图表模板，帮助团队快速完成设计与协作"
    assert 37 <= len(title) <= 60
    parsed = strict_parse_stage_d(
        {"schema_version": "stage_d_editorial_v1", "decisions": [_selected(1, title=title)]},
        event_ids=[1],
    )
    assert parsed.decisions[0].display_title_zh == title


def test_stage_d_rejects_titles_over_60_characters_without_truncation():
    title = "模型发布新能力并为开发者提供完整工作流支持与多种图表模板，帮助团队快速完成设计与协作，支持更多行业场景落地与部署验证并持续优化"
    assert len(title) > 60
    with pytest.raises(ValueError, match="display_title_zh"):
        strict_parse_stage_d(
            {"schema_version": "stage_d_editorial_v1", "decisions": [_selected(1, title=title)]},
            event_ids=[1],
        )


def test_stage_d_provider_payload_compacts_verbose_fields_without_dropping_ids():
    event = {
        "event_id": 42,
        "title": "T" * 500,
        "summary_cn": "S" * 2_000,
        "topic": "project",
        "content_class": "project_tool",
        "keywords": [f"keyword-{index}-{'x' * 100}" for index in range(40)],
        "entities": [
            {"name": f"entity-{index}-{'n' * 100}", "type": "technology", "aliases": ["a" * 100] * 10}
            for index in range(40)
        ],
        "source_groups": ["official_blog", "x_official"],
        "source_ids": ["official-source", "first-party-account"],
        "risk_flags": [f"risk-{index}-{'r' * 300}" for index in range(40)],
        "recent_daily_history": {"appeared_recently": True, "prior_editions": [str(index) for index in range(20)]},
    }
    payload = build_stage_d_provider_payload(
        [event],
        edition={"max_selected": 30},
        model="test-model",
        api_style="openai_responses",
    )
    user_message = payload["input"][1]["content"]
    compact = json.loads(user_message.split("<events>\n", 1)[1].rsplit("\n</events>", 1)[0])[0]
    assert compact["event_id"] == 42
    assert len(compact["title"]) == 240
    assert len(compact["summary_cn"]) == 720
    assert len(compact["keywords"]) == 16
    assert len(compact["entities"]) == 16
    assert len(compact["risk_flags"]) == 12
    assert compact["source_ids"] == ["official-source", "first-party-account"]


class _HTTPResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json", "x-request-id": "req-test"}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _HTTPClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, headers, json, **kwargs):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def _stage_d_provider_payload(event_id: int):
    return {"schema_version": "stage_d_editorial_v1", "decisions": [_selected(event_id)]}


def test_stage_d_http_400_is_structured_audited_and_not_retried():
    session_factory = _db()
    event_id = _event(session_factory, title="HTTP provider failure", summary="一个足够长的事件摘要用于回退标题。", score=90)
    reference = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)
    with session_factory() as session:
        run = IntelRepository(session).start_run(reference_time=reference)
        session.commit()
        run_id = int(run.id)
    http = _HTTPClient([_HTTPResponse(400, {"error": {"code": "invalid_schema", "message": "schema rejected"}})])
    client = StageDEditorialClient(
        api_url="http://127.0.0.1:8317/v1",
        api_key="key",
        model="test-model",
        api_style="openai_responses",
        http_client=http,
    )

    result = run_stage_d_job(session_factory=session_factory, run_id=run_id, event_ids=[event_id], ai_client=client)

    assert result.used_fallback is True
    assert result.provider_attempts == 1
    assert len(http.calls) == 1
    assert client.last_error_metadata["status_code"] == 400
    assert client.last_error_metadata["error_code"] == "invalid_schema"
    with session_factory() as session:
        task = (
            session.query(IntelRunStageTask)
            .join(IntelRunStageTask.stage)
            .filter(IntelRunStageTask.subject_id == str(run_id), IntelRunStage.stage_name == "stage_d")
            .one()
        )
        attempt = task.attempts[0]
        assert json.loads(attempt.raw_response_json)["body"]["error"]["code"] == "invalid_schema"
        assert json.loads(task.result_json)["provider_attempts"] == 1
        summary = build_run_snapshot_summary(session, run=session.get(IntelRun, run_id), snapshot_key="daily-2026-08-18")
        assert summary["stages"]["stage_d"]["details"]["stage_d_source"] == "deterministic_fallback"
        assert summary["stages"]["stage_d"]["details"]["provider_status_code"] == 400


def test_stage_d_transient_provider_error_retries_then_materializes_selected_only():
    event_id = 7
    http = _HTTPClient(
        [
            _HTTPResponse(429, {"error": {"code": "rate_limit", "message": "retry"}}),
            _HTTPResponse(200, _stage_d_provider_payload(event_id)),
        ]
    )
    client = StageDEditorialClient(
        api_url="https://example.test/v1",
        api_key="key",
        model="test-model",
        api_style="openai_responses",
        http_client=http,
    )
    response, attempts = _call_editorial_provider(
        client,
        [{"event_id": event_id, "title": "测试事件", "summary_cn": "测试事件摘要"}],
        edition={"max_selected": 30},
        total_max=30,
        retries=2,
    )
    assert attempts == 2
    assert len(http.calls) == 2
    assert len(response.decisions) == 1
    assert response.decisions[0].decision == "selected"


def test_stage_d_local_story_family_guard_does_not_trigger_fallback():
    session_factory = _db()
    event_ids = [
        _event(session_factory, title="Alpha model update", summary="Alpha 模型更新摘要足够长。", score=90),
        _event(session_factory, title="Beta model update", summary="Beta 模型更新摘要足够长。", score=89),
        _event(session_factory, title="Gamma model update", summary="Gamma 模型更新摘要足够长。", score=88),
    ]

    def provider_payload(events):
        decisions = []
        for order, event in enumerate(events, start=1):
            decision = _selected(int(event["event_id"]), order=order, family="same-family")
            decision["family_position"] = min(order, 2)
            decisions.append(decision)
        return {"schema_version": "stage_d_editorial_v1", "decisions": decisions}

    result = run_stage_d_job(
        session_factory=session_factory,
        event_ids=event_ids,
        ai_client=_Client(provider_payload),
    )

    assert result.used_fallback is False
    assert result.ai_failed == 0
    assert result.selected == 2
    assert result.omitted == 1
    with session_factory() as session:
        snapshots = session.query(IntelEventStageDSnapshot).order_by(IntelEventStageDSnapshot.event_id).all()
        omitted = next(row for row in snapshots if row.event_id == event_ids[2])
        metadata = json.loads(omitted.metadata_json)
        assert metadata["stage_d_source"] == "ai"
        assert "local_story_family_limit" in metadata["reason_codes"]


def test_stage_d_client_max_retries_controls_job_retry_count():
    event_id = 8
    http = _HTTPClient(
        [
            _HTTPResponse(429, {"error": {"code": "rate_limit", "message": "retry"}}),
            _HTTPResponse(429, {"error": {"code": "rate_limit", "message": "retry"}}),
            _HTTPResponse(200, _stage_d_provider_payload(event_id)),
        ]
    )
    client = StageDEditorialClient(
        api_url="https://example.test/v1",
        api_key="key",
        model="test-model",
        api_style="openai_responses",
        max_retries=2,
        http_client=http,
    )
    response, attempts = _call_editorial_provider(
        client,
        [{"event_id": event_id, "title": "测试事件", "summary_cn": "测试事件摘要"}],
        edition={"max_selected": 30},
        total_max=30,
        retries=client.max_retries,
    )
    assert attempts == 3
    assert len(http.calls) == 3
    assert response.decisions[0].decision == "selected"
