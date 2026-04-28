from datetime import datetime, timezone

from app.pipeline.prefilter import evaluate_candidate
from app.storage.models import NormalizedItem, RawItem, Source


def make_normalized_item(
    source_id="reddit_local_llama_new",
    title="New GGUF model released",
    body_text="Open weights model with GitHub repo",
    url="https://github.com/example/model",
    raw_html: str | None = None,
):
    source = Source(
        id=source_id,
        name=source_id,
        type="atom",
        url="https://example.com/feed",
        enabled=True,
        priority=10,
        fetch_interval=3600,
        parser_type="feedparser",
    )
    raw_item = RawItem(
        id=1,
        source_id=source_id,
        external_id="guid-1",
        title=title,
        link=url,
        author="author",
        published_at=datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
        raw_summary=raw_html or body_text,
        raw_content=raw_html or body_text,
        raw_payload="{}",
        content_hash="hash-1",
        status="normalized",
        source=source,
    )
    return NormalizedItem(
        id=1,
        raw_item_id=1,
        title=title,
        body_text=body_text,
        url=url,
        author="author",
        published_at=datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc),
        language="en",
        dedupe_key=f"url:{url}",
        raw_item=raw_item,
    )


def test_evaluate_candidate_keeps_ai_tool_release_with_link():
    decision = evaluate_candidate(make_normalized_item())

    assert decision.keep is True
    assert decision.score >= 60
    assert "gguf" in decision.matched_keywords
    assert "github_link" in decision.keep_reasons


def test_evaluate_candidate_drops_low_signal_chatter():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_hot",
            title="大家今天吃什么",
            body_text="随便聊聊天，没有工具，没有模型，也没有链接",
            url=None,
        )
    )

    assert decision.keep is False
    assert "low_score" in decision.drop_reasons


def test_evaluate_candidate_drops_linux_do_community_notice_with_only_generic_keywords():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_hot",
            title="请不要把互联网上的戾气带来这里！",
            body_text="社区公告，工具更新与发布规则说明，欢迎友善交流。",
            url="https://linux.do/t/topic/482293",
        )
    )

    assert decision.keep is False
    assert "low_score" in decision.drop_reasons


def test_evaluate_candidate_detects_external_repo_signal_from_linux_do_raw_html():
    decision = evaluate_candidate(
        make_normalized_item(
            source_id="linux_do_top",
            title="开源智能体工作流工具发布",
            body_text="项目支持本地模型编排。",
            url="https://linux.do/t/topic/123456",
            raw_html=(
                '<p>项目支持本地模型编排。</p>'
                '<a href="https://github.com/example/agent-workflow">GitHub repo</a>'
            ),
        )
    )

    assert decision.keep is True
    assert "external_link_signal" in decision.keep_reasons
