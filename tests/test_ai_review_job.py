from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.ai.schemas import ItemAnalysisResponse
from app.config.source_registry import SourceConfig
from app.domain.models import FetchItem
from app.domain.policies import source_spec_from_config
from app.jobs.ai_review_job import _editorial_section_for_item, run_ai_review_job
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import AIItemReview, IntelItem, IntelItemVerification
from app.storage.repository import IntelRepository


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'ai-review.db'}")
    init_db(engine)
    return create_session_factory(engine)


def _source() -> SourceConfig:
    return SourceConfig(
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

    def analyze(self, request):
        self.calls.append(request.item_id)
        if request.item_id in self.fail_ids:
            raise RuntimeError("provider timeout")
        return ItemAnalysisResponse(
            keep=True,
            content_class=request.source_content_class,
            summary_cn="中文简要总结",
            reason="保留",
            risk_flags=[],
            needs_verification=True,
            official_url=request.url,
            confidence=91,
            raw_response={"fixture": True},
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

    def forbidden(*args, **kwargs):
        raise AssertionError("verification must not run in ai-review")

    monkeypatch.setattr("app.jobs.process_job._verify", forbidden)
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
    assert record["evidence_status"] == "not_run"
    assert record["verification_status"] == "not_run"
    assert record["verification"] is None
    assert record["summary_cn"] == "中文简要总结"
    assert records_by_title["Announcing a new model release"]["editorial_section"] == "model_product"
    assert records_by_title["API version update"]["editorial_section"] == "industry_infrastructure"
    assert record["content_class"] == "official_model_company"

    with sf() as session:
        rows = session.scalars(select(IntelItem).order_by(IntelItem.id)).all()
        assert rows[0].status == "selected"
        assert rows[0].ai_review.status == "success"
        assert rows[0].ai_review.summary_cn == "中文简要总结"
        assert rows[0].verification is None
        assert rows[1].status == "filtered"
        assert rows[2].status == "selected"
        assert session.scalars(select(IntelItemVerification)).all() == []


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
    assert failed["evidence_status"] == "not_run"


def test_editorial_section_is_output_only_and_deterministic():
    assert _editorial_section_for_item(
        SimpleNamespace(
            content_class="project_tool",
            title="A database tool",
            summary="",
            content_text="",
        )
    ) == "open_source_tool"
    assert _editorial_section_for_item(
        SimpleNamespace(
            content_class="community_social",
            title="New arXiv paper benchmark",
            summary="",
            content_text="",
        )
    ) == "research"
    assert _editorial_section_for_item(
        SimpleNamespace(
            content_class="community_social",
            title="GPU cloud platform launch",
            summary="",
            content_text="",
        )
    ) == "industry_infrastructure"
    assert _editorial_section_for_item(
        SimpleNamespace(
            content_class="community_social",
            title="Practical workflow notes",
            summary="",
            content_text="",
        )
    ) == "practice_opinion"
