"""Category-4 partial-close compatibility interface.

The legacy method remains fail-closed. Pure plans use TradeRecord and
TradeManagement, and can be staged durably but never auto-submitted to OANDA.
"""
from __future__ import annotations


class PartialCloseEngine:
    def __init__(self, executor):
        self.executor = executor

    def preview(self, record, rules, *, bid, ask, phase="phase1",
                trigger_r=1, fraction=0.5):
        """Return positive partial-close units, or None if not due/unsafe."""
        return record.partial_close_units(
            rules, bid=bid, ask=ask, phase=phase,
            trigger_r=trigger_r, fraction=fraction
        )

    def check_and_execute(self):
        raise RuntimeError(
            "Real partial close is disabled; Category 4 only stages and verifies intents"
        )
