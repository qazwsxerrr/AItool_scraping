from datetime import datetime, timezone

from app.pipeline.normalize import normalize_raw_item
from app.storage.models import RawItem


def make_raw_item(**overrides):
    data = dict(
        id=1,
        source_id="source_a",
        external_id="guid-1",
        title="  <b>Launch</b>   New\n AI Tool  ",
        link="HTTPS://Example.com/Product/?utm_source=newsletter&ref=feed#section",
        author=" Ada ",
        published_at=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
        raw_summary="<p>Hello&nbsp;<strong>AI</strong> world</p>",
        raw_content="<article>Hello&nbsp;<strong>AI</strong> world<br>第二行</article>",
        raw_payload="{}",
        content_hash="hash-1",
        status="new",
    )
    data.update(overrides)
    return RawItem(**data)


def test_normalize_raw_item_cleans_text_url_and_language():
    normalized = normalize_raw_item(make_raw_item())

    assert normalized.raw_item_id == 1
    assert normalized.title == "Launch New AI Tool"
    assert normalized.body_text == "Hello AI world 第二行"
    assert normalized.url == "https://example.com/Product"
    assert normalized.author == "Ada"
    assert normalized.language == "zh"
    assert normalized.dedupe_key == "url:https://example.com/Product"


def test_normalize_raw_item_falls_back_to_title_hash_without_url():
    first = normalize_raw_item(make_raw_item(id=1, link=None, title="Same Tool", raw_content="Alpha"))
    second = normalize_raw_item(make_raw_item(id=2, link=None, title=" Same   Tool ", raw_content="Beta"))

    assert first.url is None
    assert first.dedupe_key.startswith("title:")
    assert first.dedupe_key == second.dedupe_key
