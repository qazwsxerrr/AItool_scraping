from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.ai.schemas import TriageResult
from app.domain.models import FetchItem, SourceSpec
from app.domain.policies import source_spec_from_config
from app.jobs.ai_review_job import run_ai_review_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'ai-review.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _source() -> SourceSpec:
    return SourceSpec(
        id="official_review_test",
        name="Official review test",
        transport="feed",
        url="https://official.example/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_subtype="fixed_news",
        source_role="official",
        content_class="official_model_company",
        fetch_interval=1,
    )


class _AI:
    model = "test-model"

    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids)
        self.calls: list[int] = []

    def triage(self, envelope):
        self.calls.append(envelope.item_id)
        if envelope.item_id in self.fail_ids:
            raise RuntimeError("provider timeout")
        return TriageResult(
            item_id=envelope.item_id,
            keep=True,
            topic="product",
            topics=["product"],
            summary_cn="中文简要总结",
            keywords=["model", "release"],
            selection_score=91,
            scores={"relevance": 90, "total": 91},
            novelty="new",
            paper_support={"is_paper": False},
            risk_flags=[],
            reason="保留",
            confidence=91,
            raw_response={"fixture": True},
        )


class _RejectingAI(_AI):
    def triage(self, envelope):
        self.calls.append(envelope.item_id)
        return TriageResult(
            item_id=envelope.item_id,
            keep=False,
            topic="product",
            topics=["product"],
            summary_cn="与 AI 日报主题无关",
            keywords=["noise"],
            selection_score=4,
            scores={"relevance": 2, "total": 4},
            novelty="repeat",
            paper_support={"is_paper": False},
            reason="内容与 AI 工具情报无关",
            risk_flags=["irrelevant"],
            confidence=94,
            raw_response={"fixture": True, "keep": False},
        )


def _insert_items(sf, source, spec):
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        for external_id, title in (
            ("keep:1", "Announcing a new model release"),
            ("filtered:1", "Unrelated noise"),
            ("failed:1", "API version update"),
        ):
            repo.insert_item(
                FetchItem(
                    source_id=source.id,
                    external_id=external_id,
                    title=title,
                    url=f"https://official.example/{external_id}",
                    published_at=NOW - timedelta(hours=2),
                    summary=title,
                )
            )
        session.commit()


def test_ai_review_never_calls_verification_and_exports_not_run(tmp_path, monkeypatch):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    _insert_items(sf, source, spec)
    ai = _AI()

    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: spec},
        ai_client=ai,
        output_dir=tmp_path / "out",
        limit=10,
        now=NOW,
    )

    assert result.analyzed == 2
    assert result.filtered == 1
    assert result.exported == 2
    records = [
        json.loads(line)
        for line in (tmp_path / "out" / "ai_review_candidates.jsonl").read_text().splitlines()
    ]
    records_by_title = {row["title"]: row for row in records}
    record = records[0]
    assert record["stage"] == "ai_review"
    assert record["source_id"] == source.id
    assert record["source_group"] == "official_blog"
    assert record["source_subtype"] == "fixed_news"
    assert "evidence_status" not in record
    assert "verification_status" not in record
    assert "verification" not in record
    assert record["summary_cn"] == "中文简要总结"
    assert record["content_class"] == "official_model_company"

    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert rows[0].status == "selected"
        assert rows[0].ai_review.status == "success"
        assert rows[0].ai_review.summary_cn == "中文简要总结"
        assert rows[0].ai_review.topic == "product"
        assert json.loads(rows[0].ai_review.topics_json) == ["product"]
        assert json.loads(rows[0].ai_review.keywords_json) == ["model", "release"]
        assert rows[0].ai_review.selection_score == 91
        assert rows[0].ai_review.novelty == "new"
        assert json.loads(rows[0].ai_review.paper_support_json)["is_paper"] is False
        assert rows[1].status == "filtered"
        assert rows[2].status == "selected"


def test_ai_review_failure_isolated_and_auditable(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    _insert_items(sf, source, spec)
    with sf() as session:
        failed_item = session.scalar(select(IntelItem).where(IntelItem.external_id == "failed:1"))
        assert failed_item is not None
        failed_id = failed_item.id

    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: spec},
        ai_client=_AI(fail_ids={failed_id}),
        output_dir=tmp_path / "out",
        limit=10,
        now=NOW,
    )
    assert result.ai_failed == 1
    assert result.analyzed == 1

    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert rows[0].status == "selected"
        assert rows[1].status == "filtered"
        assert rows[2].status == "ai_failed"
        failed_review = session.scalar(select(AIItemReview).where(AIItemReview.item_id == failed_id))
        assert failed_review is not None
        assert failed_review.status == "ai_failed"
        assert failed_review.error_message == "provider timeout"

    audit = [json.loads(line) for line in (tmp_path / "out" / "ai_review_audit.jsonl").read_text().splitlines()]
    assert {row["status"] for row in audit} == {"selected", "filtered", "ai_failed"}
    failed = next(row for row in audit if row["status"] == "ai_failed")
    assert failed["ai"]["status"] == "ai_failed"
    assert "evidence_status" not in failed


def test_ai_review_ai_keep_false_rejects_item_and_excludes_candidate(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="irrelevant:1",
                title="Announcing a new model release",
                url="https://official.example/irrelevant",
                published_at=NOW - timedelta(hours=2),
                summary="A deliberately irrelevant entertainment item unrelated to the claimed release.",
            )
        )
        session.commit()

    ai = _RejectingAI()
    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: spec},
        ai_client=ai,
        output_dir=tmp_path / "out",
        limit=10,
        now=NOW,
    )

    assert result.selected == 1
    assert result.analyzed == 1
    assert result.exported == 0
    assert ai.calls == [1]

    with sf() as session:
        item = session.scalar(select(IntelItem))
        assert item is not None
        assert item.status == "rejected"
        assert item.ai_review is not None
        assert item.ai_review.status == "success"
        assert item.ai_review.keep is False

    audit = [json.loads(line) for line in (tmp_path / "out" / "ai_review_audit.jsonl").read_text().splitlines()]
    assert len(audit) == 1
    assert audit[0]["status"] == "rejected"
    assert audit[0]["keep_decision"] is False
    assert audit[0]["ai"]["risk_flags"] == ["irrelevant"]


def test_ai_review_p1_official_without_keyword_reaches_ai_and_exports_summary(tmp_path):
    sf = _db(tmp_path)
    source = SourceSpec(
        id="official_p1_review_test",
        name="Official P1 review test",
        transport="feed",
        url="https://official.example/feed.xml",
        feed={"format": "rss", "adapter": "generic"},
        source_group="official_blog",
        source_subtype="fixed_news",
        source_role="official",
        content_class="official_model_company",
        tier="p1",
        fetch_interval=1,
    )
    spec = source_spec_from_config(source)
    with sf() as session:
        repo = IntelRepository(session)
        repo.upsert_source(source, policy=spec)
        repo.insert_item(
            FetchItem(
                source_id=source.id,
                external_id="openai:assistance-to-execution",
                title="From assistance to execution: building agents",
                url="https://official.example/assistance-to-execution",
                published_at=NOW - timedelta(hours=2),
                summary="A first-party article about building agents.",
            )
        )
        session.commit()

    ai = _AI()
    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: spec},
        ai_client=ai,
        output_dir=tmp_path / "out",
        limit=10,
        now=NOW,
    )

    assert result.analyzed == 1
    assert ai.calls == [1]
    record = json.loads((tmp_path / "out" / "ai_review_candidates.jsonl").read_text().splitlines()[0])
    assert record["summary_cn"] == "中文简要总结"
    assert record["selection_reason"] == "selected:official_recent_no_keyword; risks=official_keyword_missing"


def test_ai_review_export_keeps_source_attribution(tmp_path):
    sf = _db(tmp_path)
    source = _source()
    spec = source_spec_from_config(source)
    _insert_items(sf, source, spec)
    result = run_ai_review_job(
        session_factory=sf,
        source_specs={source.id: spec},
        ai_client=_AI(),
        output_dir=tmp_path / "out",
        limit=10,
        now=NOW,
    )
    record = json.loads((tmp_path / "out" / "ai_review_candidates.jsonl").read_text().splitlines()[0])
    assert result.exported == 2
    assert record["source_name"] == source.name
    assert record["source_group"] == "official_blog"
    assert record["x_official"] is False
