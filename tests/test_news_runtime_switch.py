"""Runtime live-news switch: temporary disable must make no provider call."""
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

import config
from main import _live_news_status


NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


class LiveNewsRuntimeSwitchTests(TestCase):
    def test_disabled_by_default_and_provider_not_instantiated(self):
        self.assertIs(config.ENABLE_NEWS_FILTER, False)
        with patch("main.build_news_filter") as build:
            self.assertIsNone(_live_news_status("EUR_USD", NOW))
            build.assert_not_called()

    def test_enabled_uses_original_news_unknown_fail_closed(self):
        with (patch.object(config, "ENABLE_NEWS_FILTER", True),
              patch("main.build_news_filter") as build):
            build.return_value.block_trading.return_value = "NEWS_UNKNOWN: no coverage"
            self.assertEqual(_live_news_status("EUR_USD", NOW),
                             "NEWS_UNKNOWN: no coverage")
            build.return_value.block_trading.assert_called_once_with("EUR_USD", NOW)

    def test_enabled_preserves_high_impact_lockout(self):
        with (patch.object(config, "ENABLE_NEWS_FILTER", True),
              patch("main.build_news_filter") as build):
            build.return_value.block_trading.return_value = "HIGH-impact lockout USD"
            self.assertEqual(_live_news_status("EUR_USD", NOW),
                             "HIGH-impact lockout USD")
