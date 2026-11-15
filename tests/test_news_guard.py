import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from data.news_guard import NewsGuard


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.json"
        self.now = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, generated_at=None, events=None):
        self.path.write_text(json.dumps({
            "generated_at": (generated_at or self.now).isoformat(),
            "events": events if events is not None else []}), encoding="utf-8")

    def test_unknown_feed_blocks_entries(self):
        g = NewsGuard(mode="ENABLED_JSON", path=self.path)
        self.assertEqual(g.check("EUR_USD", self.now), (False, "SOURCE_UNKNOWN"))
        self.assertEqual(NewsGuard().check("EUR_USD", self.now), (False, "UNCONFIGURED"))

    def test_high_impact_currency_and_timezone(self):
        self.save(events=[{"time": (self.now + timedelta(minutes=15)).isoformat(),
                           "impact": "HIGH", "currency": "USD"}])
        g = NewsGuard(mode="ENABLED_JSON", path=self.path)
        self.assertEqual(g.check("EUR_USD", self.now), (False, "HIGH_IMPACT"))
        self.assertEqual(g.check("EUR_GBP", self.now), (True, "VERIFIED_NO_BLOCKING_EVENT"))

    def test_expired_or_malformed_feed_blocks(self):
        self.save(generated_at=self.now - timedelta(hours=1))
        g = NewsGuard(mode="ENABLED_JSON", path=self.path, cache_seconds=120)
        self.assertEqual(g.check("EUR_USD", self.now), (False, "SOURCE_UNKNOWN"))
        self.path.write_text('{"generated_at":"bad","events":[]}')
        self.assertEqual(g.check("EUR_USD", self.now), (False, "SOURCE_UNKNOWN"))

    def test_deliberate_disabled_mode(self):
        self.assertEqual(NewsGuard(mode="DISABLED_ACKNOWLEDGED").check("EUR_USD", self.now),
                         (True, "DISABLED_EXPLICITLY"))


if __name__ == "__main__":
    unittest.main()
