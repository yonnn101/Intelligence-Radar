import asyncio
import unittest

import httpx

from monitoring import fetch_rss


class MonitoringTests(unittest.TestCase):
    def test_invalid_feed_raises(self):
        async def run():
            async def handler(request):
                return httpx.Response(200, text="<not xml")

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(ValueError):
                    await fetch_rss(client, {"locator": "https://example.test/feed", "name": "Broken"})

        asyncio.run(run())

    def test_rss_item_is_normalized(self):
        async def run():
            xml = """<?xml version="1.0"?><rss version="2.0"><channel>
            <title>Example</title><item><title>Security update</title>
            <link>https://example.test/item</link><description><![CDATA[<b>Details</b>]]></description>
            </item></channel></rss>"""

            async def handler(request):
                return httpx.Response(200, text=xml)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                items = await fetch_rss(client, {"locator": "https://example.test/feed", "name": "Example"})
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].title, "Security update")
                self.assertEqual(items[0].content, "Details")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
