"""Phase E / Category 25: transfer-neutral daily loss and zero-P/L streaks.

All fixtures are synthetic; no broker connectivity or writes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from analytics.performance import report as performance_report
from broker.financial_ledger import (
    FinancialEvidenceError, initial_ledger, parse_transaction,
    record_transaction, summary, validate_ledger,
)
from risk.reconciled_account import (
    RiskEvidenceError, RiskHistoryAnchor, _losses_from_anchor,
    _transaction_downside,
)
from risk.trade_plan import AccountFunds, PlanRejected, advance_loss_streak


D = Decimal
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def _fill(transaction_id, *, opened=False, pl="0", commission="0",
          financing="0"):
    tx = {
        "id": str(transaction_id), "type": "ORDER_FILL", "pl": pl,
        "financing": financing, "commission": commission,
        "guaranteedExecutionFee": "0",
    }
    if opened:
        tx["tradeOpened"] = {
            "tradeID": "11", "units": "100", "guaranteedExecutionFee": "0",
        }
    else:
        tx["tradesClosed"] = [{
            "tradeID": "11", "units": "-100", "realizedPL": pl,
            "financing": financing, "guaranteedExecutionFee": "0",
        }]
    return tx


def _anchored_losses(*, close_pl="-10", open_commission="2",
                     close_financing="-1", transfer=None, start_streak=0,
                     nav="9950", peak="10000"):
    ledger = initial_ledger("100")
    record_transaction(ledger, _fill(
        101, opened=True, commission=open_commission,
    ))
    record_transaction(ledger, _fill(
        102, pl=close_pl, financing=close_financing,
    ))
    cursor = "102"
    if transfer is not None:
        record_transaction(ledger, {
            "id": "103", "type": "TRANSFER_FUNDS", "amount": transfer,
        })
        cursor = "103"
    validate_ledger(
        ledger, cursor=cursor,
        processed=[str(x) for x in range(101, int(cursor) + 1)],
    )
    state = {
        "cursor": cursor, "financial_ledger": ledger,
        "closed_results": {"11": {
            "transaction_id": "102", "instrument": "EUR_USD",
            "closed_at": "2026-11-16T10:00:00Z", "pl": close_pl,
        }},
        "cooldowns": {},
    }
    account = AccountFunds(
        "acct-1", "USD", D(nav), D(nav), D("5000"), D("0"),
        0, 0, cursor,
    )
    anchor = RiskHistoryAnchor(
        account_id="acct-1", cursor=cursor, day="2026-11-16",
        day_start_cursor="100", day_start_nav=D("10000"),
        peak_nav=D(peak), starting_loss_streak=start_streak,
        history_complete=True,
    )
    result, blockers = _losses_from_anchor(account, state, anchor, now=NOW)
    return result, blockers, ledger


class TransferLossTests(unittest.TestCase):
    def test_valid_deposit_and_withdrawal_are_not_trading_debits(self):
        for amount in ("5000", "-5000", "0"):
            with self.subTest(amount=amount):
                event = parse_transaction({
                    "id": "103", "type": "TRANSFER_FUNDS", "amount": amount,
                })
                self.assertIsNone(event["account_net"])
                self.assertEqual(_transaction_downside(event), D("0"))

    def test_deposit_does_not_hide_real_daily_loss(self):
        losses, blocked, ledger = _anchored_losses(
            transfer="500", nav="10450", peak="10500",
        )
        self.assertEqual(blocked, ())
        self.assertTrue(losses.verified)
        self.assertEqual(losses.daily_realized_loss_home, D("13"))
        self.assertEqual(losses.consecutive_losses, 1)
        self.assertEqual(summary(ledger)["excluded_funds_transfer_ids"], ("103",))

    def test_withdrawal_is_not_a_new_trading_loss(self):
        losses, blocked, _ = _anchored_losses(transfer="-500", nav="9450")
        self.assertEqual(blocked, ())
        self.assertTrue(losses.verified)
        self.assertEqual(losses.daily_realized_loss_home, D("13"))
        self.assertEqual(losses.consecutive_losses, 1)

    def test_forged_transfer_with_account_net_must_not_be_ignored(self):
        event = parse_transaction({
            "id": "103", "type": "TRANSFER_FUNDS", "amount": "-500",
        })
        event["account_net"] = "-500"
        with self.assertRaises(RiskEvidenceError):
            _transaction_downside(event)

    def test_invalid_transfer_or_unknown_financial_event_remains_blocked(self):
        with self.assertRaises(FinancialEvidenceError):
            parse_transaction({
                "id": "103", "type": "TRANSFER_FUNDS", "amount": "NaN",
            })
        with self.assertRaises(RiskEvidenceError):
            _transaction_downside(parse_transaction({
                "id": "104", "type": "FUTURE_FINANCIAL_TRANSACTION",
            }))

    def test_incomplete_or_misclassified_transfer_fails_closed(self):
        missing_amount = parse_transaction({
            "id": "103", "type": "TRANSFER_FUNDS",
        })
        with self.assertRaises(RiskEvidenceError):
            _transaction_downside(missing_amount)
        injected_financing = parse_transaction({
            "id": "104", "type": "TRANSFER_FUNDS", "amount": "100",
        })
        injected_financing["components"]["financing"] = "-10"
        with self.assertRaises(RiskEvidenceError):
            _transaction_downside(injected_financing)


class ConsecutiveLossTests(unittest.TestCase):
    def test_shared_streak_rule_rejects_invalid_and_preserves_neutral(self):
        streak = 0
        for net, expected in (
            ("-5", 1), ("0", 1), ("-2", 2), ("0", 2),
            ("0.01", 0), ("0", 0), ("-1", 1),
        ):
            streak = advance_loss_streak(streak, net)
            self.assertEqual(streak, expected)
        for previous, net in ((True, "0"), (-1, "1"), (0, "NaN")):
            with self.subTest(previous=previous, net=net), self.assertRaises(PlanRejected):
                advance_loss_streak(previous, net)

    def test_break_even_keeps_previous_verified_loss_streak(self):
        losses, blocked, _ = _anchored_losses(
            close_pl="0", open_commission="0", close_financing="0",
            start_streak=2, nav="10000",
        )
        self.assertEqual(blocked, ())
        self.assertTrue(losses.verified)
        self.assertEqual(losses.daily_realized_loss_home, D("0"))
        self.assertEqual(losses.consecutive_losses, 2)

    def test_positive_profit_resets_streak_and_negative_net_increments(self):
        profit, blocked, _ = _anchored_losses(
            close_pl="5", open_commission="0", close_financing="0",
            start_streak=2, nav="10005", peak="10005",
        )
        self.assertEqual(blocked, ())
        self.assertEqual(profit.consecutive_losses, 0)
        loss, blocked, _ = _anchored_losses(start_streak=2)
        self.assertEqual(blocked, ())
        self.assertEqual(loss.consecutive_losses, 3)

    def test_zero_gross_with_fees_is_still_a_real_net_loss(self):
        losses, blocked, _ = _anchored_losses(
            close_pl="0", open_commission="2", close_financing="0",
            start_streak=2,
        )
        self.assertEqual(blocked, ())
        self.assertEqual(losses.daily_realized_loss_home, D("2"))
        self.assertEqual(losses.consecutive_losses, 3)

    def test_break_even_and_withdrawal_remain_neutral_together(self):
        losses, blocked, _ = _anchored_losses(
            close_pl="0", open_commission="0", close_financing="0",
            transfer="-500", start_streak=2, nav="9500",
        )
        self.assertEqual(blocked, ())
        self.assertTrue(losses.verified)
        self.assertEqual(losses.daily_realized_loss_home, D("0"))
        self.assertEqual(losses.consecutive_losses, 2)

    def test_research_max_loss_streak_treats_zero_as_neutral(self):
        rows = [{
            "trade_id": str(i), "instrument": "EUR_USD",
            "session": "London", "net_pl": x,
        } for i, x in enumerate(("-2", "0", "-3", "1"), start=1)]
        result = performance_report(rows, starting_equity="10000")
        self.assertEqual(result["max_loss_streak"], 2)
        self.assertEqual(result["max_win_streak"], 1)


if __name__ == "__main__":
    unittest.main()
