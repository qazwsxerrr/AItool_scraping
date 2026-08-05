from datetime import datetime, timezone

from app.pipeline.normalize import build_dedupe_key, clean_text, normalize_url, standardize_item


def test_normalize_helpers_clean_feed_shaped_data():
    item = standardize_item(
        {
            "id": "item-1",
            "title": "  New <b>AI</b> tool ",
            "link": "https://Example.com/path/?utm_source=rss&x=1",
            "raw_summary": "Summary",
            "raw_content": "<p>Full content</p>",
            "published_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
    )
    assert item.item_id == "item-1"
    assert item.title == "New AI tool"
    assert item.body_text == "Full content"
    assert item.url == "https://example.com/path?x=1"
    assert item.language == "en"
    assert item.dedupe_key.startswith("url:")


def test_normalize_helpers_handle_empty_values():
    assert clean_text("<p> \n </p>") is None
    assert normalize_url(None) is None
    assert build_dedupe_key(title="Same title", url=None) == build_dedupe_key(title="Same title", url=None)
