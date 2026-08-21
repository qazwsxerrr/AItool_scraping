from datetime import timezone

from app.parsers.feed_parser import parse_feed


RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example RSS</title>
    <item>
      <guid isPermaLink="false">rss-guid-1</guid>
      <title>New AI Tool</title>
      <link>https://example.com/tools/new-ai-tool</link>
      <author>editor@example.com</author>
      <pubDate>Tue, 21 Apr 2026 10:00:00 GMT</pubDate>
      <description><![CDATA[Short <b>summary</b>]]></description>
      <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/"><![CDATA[Full content]]></content:encoded>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <id>tag:example.com,2026:atom-1</id>
    <title>Agent Framework Update</title>
    <link href="https://example.com/blog/agent-framework" />
    <author><name>Ada</name></author>
    <updated>2026-04-20T08:30:00Z</updated>
    <summary>Atom summary</summary>
    <content type="html">Atom full content</content>
  </entry>
</feed>
"""

INVALID_DATE_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Invalid date fixture</title>
    <item>
      <guid>invalid-date-1</guid>
      <title>Entry with a malformed publication date</title>
      <link>https://example.com/invalid-date</link>
      <pubDate>Invalid Date</pubDate>
      <lastBuildDate>2026-08-20T08:30:00Z</lastBuildDate>
      <description>Still useful content</description>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_item_extracts_canonical_fields_and_payload():
    items = parse_feed(RSS_SAMPLE, source_id="example_rss")

    assert len(items) == 1
    item = items[0]
    assert item.source_id == "example_rss"
    assert item.external_id == "rss-guid-1"
    assert item.title == "New AI Tool"
    assert item.link == "https://example.com/tools/new-ai-tool"
    assert item.author == "editor@example.com"
    assert item.published_at is not None
    assert item.published_at.tzinfo == timezone.utc
    assert item.raw_summary == "Short <b>summary</b>"
    assert item.raw_content == "Full content"
    assert item.content_hash
    assert item.raw_payload["title"] == "New AI Tool"


def test_parse_atom_item_uses_updated_when_published_is_missing():
    items = parse_feed(ATOM_SAMPLE, source_id="example_atom")

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "tag:example.com,2026:atom-1"
    assert item.title == "Agent Framework Update"
    assert item.link == "https://example.com/blog/agent-framework"
    assert item.author == "Ada"
    assert item.published_at is not None
    assert item.published_at.isoformat() == "2026-04-20T08:30:00+00:00"
    assert item.raw_summary == "Atom summary"
    assert item.raw_content == "Atom full content"


def test_parse_feed_keeps_entry_when_one_date_field_is_malformed():
    items = parse_feed(INVALID_DATE_SAMPLE, source_id="invalid_date")

    assert len(items) == 1
    assert items[0].title == "Entry with a malformed publication date"
    assert items[0].published_at is not None
    assert items[0].published_at.isoformat() == "2026-08-20T08:30:00+00:00"
