from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config.source_registry import SourceConfig
from app.jobs.prefilter_job import run_prefilter_job
from app.jobs.review_export_job import run_review_export_job
from app.parsers.feed_parser import ParsedFeedItem
from app.pipeline.normalize import normalize_raw_item
from app.storage.db import create_engine_from_url, create_session_factory, init_db
from app.storage.models import RawItem
from app.storage.repository import NormalizedItemRepository, RawItemRepository, SourceRepository


def make_source(source_id="reddit_local_llama_new"):
    return SourceConfig(
        id=source_id,
        name=source_id,
        type="atom",
        url="https://example.com/feed.rss",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
        source_group="reddit_local_llama",
        source_subtype="fixed_new",
        default_limit=40,
    )


def make_item(source_id, external_id, title, link, content_hash, body="body"):
    return ParsedFeedItem(
        source_id=source_id,
        external_id=external_id,
        title=title,
        link=link,
        author="author",
        published_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
        raw_summary=body,
        raw_content=body,
        raw_payload={"id": external_id},
        content_hash=content_hash,
    )


def seed_candidates(session):
    source = make_source()
    SourceRepository(session).upsert_source(source)
    raw_repo = RawItemRepository(session)
    normalized_repo = NormalizedItemRepository(session)
    kept_raw_id = raw_repo.insert_if_new(
        make_item(
            source.id,
            "keep-1",
            "Released open weights GGUF model",
            "https://huggingface.co/example/model",
            "hash-keep",
            "New benchmark and repo release",
        )
    ).item_id
    dropped_raw_id = raw_repo.insert_if_new(
        make_item(source.id, "drop-1", "A small help question", None, "hash-drop", "How do I fix my laptop?")
    ).item_id
    session.flush()
    for raw_id in [kept_raw_id, dropped_raw_id]:
        raw_item = session.get(RawItem, raw_id)
        normalized_repo.insert_if_new(normalize_raw_item(raw_item))
        raw_repo.mark_status(raw_id, "normalized")
    session.commit()


def test_review_export_job_writes_markdown_and_jsonl_for_kept_candidates(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'review.db'}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        seed_candidates(session)
    run_prefilter_job(session_factory=session_factory, limit=10)

    result = run_review_export_job(session_factory=session_factory, output_dir=tmp_path / "out", limit=10)

    assert result.exported == 1
    assert result.markdown_path.exists()
    assert result.jsonl_path.exists()

    markdown = result.markdown_path.read_text(encoding="utf-8")
    assert "# AI 初筛前人工审阅候选" in markdown
    assert "Released open weights GGUF model" in markdown
    assert "A small help question" not in markdown
    assert "## 1." in markdown

    lines = result.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["candidate_id"]
    assert payload["title"] == "Released open weights GGUF model"
    assert payload["source_group"] == "reddit_local_llama"
    assert payload["status"] == "kept"
    assert payload["body_preview"]
