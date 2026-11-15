"""P71 complete explicit account-home cost components, no hidden double count.

Price-basis contract:
EXECUTABLE_FILLS: realizedPL already reflects bid/ask execution and slippage;
spread/slippage fields are informational, NEVER subtracted again.
MID_REFERENCE: simulated mid-based P/L must subtract both cost estimates.

Commission is a positive charge; financing and dividend are signed. Missing
components are UNKNOWN (not zero), so the cost record is rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class CostError(ValueError):
    """Costs are unverified, incomplete or duplicated."""


def amount(value, key, *, nonnegative=False):
    if value is None or isinstance(value, bool):
        raise CostError(f"Missing or invalid {key}")
    try:
        x = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CostError(f"Invalid {key}") from exc
    if not x.is_finite() or (nonnegative and x < 0):
        raise CostError(f"Invalid {key}")
    return x


@dataclass(frozen=True)
class CostRecord:
    transaction_id: str
    trade_id: str
    instrument: str
    price_basis: str
    realized_pl_home: Decimal
    financing_home: Decimal
    commission_home: Decimal
    guaranteed_fee_home: Decimal
    dividend_adjustment_home: Decimal
    spread_cost_home: Decimal
    slippage_cost_home: Decimal

    def __post_init__(self):
        import re
        if (not isinstance(self.transaction_id, str)
            or not self.transaction_id.isascii()
            or not self.transaction_id.isdecimal()
            or not isinstance(self.trade_id, str)
            or not self.trade_id.isascii()
            or not self.trade_id.isdecimal()
            or not isinstance(self.instrument, str)
            or not re.fullmatch(r"[A-Z0-9]{3,8}_[A-Z0-9]{3,8}", self.instrument)):
            raise CostError("No valid broker transaction/trade/instrument identity")
        if self.price_basis not in ("EXECUTABLE_FILLS", "MID_REFERENCE"):
            raise CostError("Unknown source price basis")
        for key in (
            "realized_pl_home", "financing_home", "commission_home",
            "guaranteed_fee_home", "dividend_adjustment_home",
            "spread_cost_home", "slippage_cost_home"
        ):
            x = amount(
                getattr(self, key), key,
                nonnegative=key in (
                    "commission_home", "guaranteed_fee_home",
                    "spread_cost_home", "slippage_cost_home"
                )
            )
            object.__setattr__(self, key, x)

    @property
    def net_home(self) -> Decimal:
        pnl = (
            self.realized_pl_home + self.financing_home
            - self.commission_home - self.guaranteed_fee_home
            + self.dividend_adjustment_home
        )
        if self.price_basis == "MID_REFERENCE":
            pnl -= self.spread_cost_home + self.slippage_cost_home
        return pnl

    @property
    def informative_costs(self):
        return {
            "spread_in_execution_price": self.price_basis == "EXECUTABLE_FILLS",
            "spread_cost_home": str(self.spread_cost_home),
            "slippage_cost_home": str(self.slippage_cost_home),
        }


class CostLedger:
    """Unique transaction per trade in the local attribution model.

    A broker transaction that affects *multiple* trades must be allocated
    explicitly outside this simple ledger, never copied to two trades.
    """

    def __init__(self):
        self._rows = {}

    def register(self, record: CostRecord) -> bool:
        if not isinstance(record, CostRecord):
            raise CostError("Missing complete verified cost record")
        other = self._rows.get(record.transaction_id)
        if other is not None:
            if other == record:
                return False
            raise CostError("Conflicting/duplicate transaction or fee allocation")
        self._rows[record.transaction_id] = record
        return True

    @property
    def records(self):
        return tuple(self._rows.values())

    @property
    def net_home(self):
        return sum((r.net_home for r in self._rows.values()), Decimal("0"))

    def for_trade(self, trade_id):
        return tuple(x for x in self._rows.values() if x.trade_id == trade_id)

    def verify_complete_trade(self, trade_id, transaction_ids):
        if (not isinstance(transaction_ids, (tuple, list))
            or not transaction_ids or len(set(transaction_ids)) != len(transaction_ids)):
            raise CostError("Missing/duplicated trade cost transaction list")
        actual = {r.transaction_id for r in self.for_trade(trade_id)}
        if actual != set(transaction_ids):
            raise CostError("Missing/extraneous trade cost components")
        return sum((r.net_home for r in self.for_trade(trade_id)), Decimal("0"))
