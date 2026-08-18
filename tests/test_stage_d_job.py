from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.ai.skills.stage_d_editorial import strict_parse_stage_d
from app.jobs.stage_d_job import StageDProfile, run_stage_d_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelEvent, IntelEventStageDSnapshot, IntelItem, Source
from app.storage.repository import IntelRepository


def _db():
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _event(session_factory, *, title: str, summary: str, score: int = 80, topic: str = "model", community: bool = False, arxiv: bool = False) -> int:
    with session_factory() as session:
        source = Source(
            id=f"source-{title[:8]}",
            name="test source",
            transport="feed",
            url="https://example.test/feed",
            source_group="reddit_fixed" if community else "official_blog",
            content_class="community_social" if community else "official_model_company",
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


def _selected(event_id: int, *, order: int = 1, family: str = "story_01", title: str = "测试模型发布新能力"):
    return {
        "event_id": event_id,
        "decision": "selected",
        "display_order": order,
        "editorial_score": 90,
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


def test_stage_d_schema_rejects_overfilled_story_family_and_invalid_title():
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


def test_stage_d_schema_requires_material_update_for_recently_displayed_event():
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

    with pytest.raises(ValueError, match="material_update"):
        strict_parse_stage_d(response, event_ids=[1], events=[event])

    response["decisions"][0]["reason_codes"].append("material_update")
    parsed = strict_parse_stage_d(response, event_ids=[1], events=[event])
    assert parsed.decisions[0].decision == "selected"


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
