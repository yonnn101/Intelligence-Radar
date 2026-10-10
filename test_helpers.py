"""Lightweight tests for the dependency-free database helpers."""
import tempfile
import unittest
from pathlib import Path

from db import Database, _keywords
from relevance import score_item
from bot import digest_to_html


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "test.db"))
        self.db.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_keywords_remove_common_words_and_deduplicate(self):
        words = _keywords("What did we talk about SAST tools and SAST tools?")
        self.assertIn("sast", words)
        self.assertIn("tools", words)
        self.assertEqual(len(words), len(set(words)))

    def test_insert_article_deduplicates_urls(self):
        payload = dict(
            url="https://example.com/post", title="Test story", source="Example",
            category="cybersecurity", published_at="2026-10-10T10:00:00+00:00",
            content="A sample story about an advisory.", baseline=True,
        )
        first = self.db.insert_article(**payload)
        second = self.db.insert_article(**payload)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_persistent_conversation_memory(self):
        self.db.store_message(123, "user", "We discussed SAST testing.")
        self.db.store_message(123, "assistant", "Start with Semgrep and CodeQL.")
        recent = self.db.recent_messages(123)
        memories = self.db.search_older_memories(123, "SAST testing", skip_latest=1)
        self.assertEqual(len(recent), 2)
        self.assertTrue(any("SAST" in m["content"] for m in recent))
        self.assertTrue(memories)

    def test_digest_time_window_includes_new_article(self):
        self.db.insert_article(
            url="https://example.com/recent", title="Recent update", source="Example",
            category="startup", published_at="2026-10-10T10:00:00+00:00",
            content="Recent test item", baseline=True,
        )
        found = self.db.recent_articles(limit=10, hours=24)
        self.assertTrue(any(item["title"] == "Recent update" for item in found))

    def test_source_registry_persists_and_toggles(self):
        source_id = self.db.add_source(
            name="CISA", source_type="rss", locator="https://example.com/cisa",
            categories="cybersecurity,cve", priority=5,
        )
        self.assertIsNotNone(source_id)
        self.assertFalse(self.db.add_source(
            name="CISA", source_type="rss", locator="https://example.com/other",
            categories="general", priority=1,
        ))
        self.assertTrue(self.db.set_source_enabled("CISA", False))
        source = self.db.list_sources()[0]
        self.assertEqual(source["enabled"], 0)
        self.assertTrue(self.db.remove_source("CISA"))

    def test_cross_source_title_deduplication(self):
        first = self.db.insert_article(
            url="https://one.example/item", title="Critical CVE affects products",
            source="One", category="cybersecurity", published_at="2026-10-10T10:00:00+00:00",
            content="CVE-2026-1234", baseline=False,
        )
        second = self.db.insert_article(
            url="https://two.example/item", title="Critical CVE affects products",
            source="Two", category="cybersecurity", published_at="2026-10-10T10:01:00+00:00",
            content="CVE-2026-1234", baseline=False,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_relevance_score_explains_high_severity_match(self):
        result = score_item(
            title="Critical CVE actively exploited in Ethiopia",
            content="Remote code execution vulnerability and incident response guidance.",
            category="cybersecurity", source_priority=5,
            published_at="2026-10-10T10:00:00+00:00",
        )
        self.assertEqual(result.level, "high")
        self.assertIn("severity signal", result.reason)
        self.assertIn("CVE-2026", "CVE-2026 " + " ".join(result.entities))

    def test_notification_threshold_filters_low_relevance_items(self):
        self.db.insert_article(
            url="https://example.com/low", title="Unrelated local update", source="Example",
            category="general", published_at="2026-10-10T10:00:00+00:00",
            content="A general update.", baseline=False, relevance_score=10,
            relevance_level="low",
        )
        self.db.insert_article(
            url="https://example.com/high", title="Critical CVE update", source="Example",
            category="cybersecurity", published_at="2026-10-10T10:00:00+00:00",
            content="A vulnerability exploit.", baseline=False, relevance_score=80,
            relevance_level="high",
        )
        pending = self.db.pending_articles(limit=10, minimum_score=65)
        self.assertEqual([item["title"] for item in pending], ["Critical CVE update"])

    def test_digest_markdown_is_rendered_as_telegram_html(self):
        rendered = digest_to_html("### Security\n\n**CVE-2026-1234**\n\nhttps://example.com/advisory")
        self.assertIn("<b>Security</b>", rendered)
        self.assertIn("<b>CVE-2026-1234</b>", rendered)
        self.assertIn("https://example.com/advisory", rendered)


if __name__ == "__main__":
    unittest.main()
