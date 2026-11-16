"""P70/P71 deterministic OHLC stop-first accounting; NOT live execution.

Prices are explicit bid/ask-side execution assumptions. Costs have unique
components and are deducted once. Never infer intrabar order of SL and TP.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation

class BacktestError(ValueError): pass

def dec(value,name):
    if isinstance(value,bool): raise BacktestError("Invalid "+name)
    try: d=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError) as exc:
        raise BacktestError("Invalid "+name) from exc
    if not d.is_finite(): raise BacktestError("Nonfinite "+name)
    return d

@dataclass(frozen=True)
class FillCosts:
    spread: Decimal=Decimal("0")
    slippage: Decimal=Decimal("0")
    commission: Decimal=Decimal("0")
    financing: Decimal=Decimal("0")
    def total(self):
        parts=[dec(getattr(self,k),k) for k in
               ("spread","slippage","commission","financing")]
        if any(x<0 for x in parts): raise BacktestError("Negative cost")
        return sum(parts,Decimal("0"))

def stop_first_bar(direction, *,low,high,stop,target):
    """Return SL/TP/OPEN. Same-bar double touch ALWAYS SL."""
    if direction not in ("LONG","SHORT"): raise BacktestError("Direction")
    lo,hi,sl,tp=(dec(x,k) for x,k in zip(
        (low,high,stop,target),("low","high","stop","target")))
    if lo>hi or min(lo,hi,sl,tp)<=0: raise BacktestError("OHLC bounds")
    if direction=="LONG":
        if sl>=tp: raise BacktestError("Long stop/target ordering")
        if lo<=sl: return "SL"
        if hi>=tp: return "TP"
    else:
        if sl<=tp: raise BacktestError("Short stop/target ordering")
        if hi>=sl: return "SL"
        if lo<=tp: return "TP"
    return "OPEN"

def net_result(*, gross_pl, costs: FillCosts, gross_includes=None):
    """Compute net P/L only from explicitly attested cost inclusion.

    Do NOT apply this to an OANDA realizedPL value by guessing which costs
    are already included. In particular, spread may already be reflected in
    the execution prices. Missing cost provenance must block calculations.
    """
    if not isinstance(costs, FillCosts):
        raise BacktestError("Explicit cost breakdown required")
    if gross_includes is None or isinstance(gross_includes, str):
        raise BacktestError("Gross cost inclusions must be explicitly attested")
    try:
        included = tuple(gross_includes)
        included_set = set(included)
    except (TypeError, ValueError) as exc:
        raise BacktestError("Invalid gross inclusion attestation") from exc
    allowed = {"spread", "slippage", "commission", "financing"}
    if len(included) != len(included_set) or not included_set <= allowed:
        raise BacktestError("Unknown or duplicated included cost")
    gross = dec(gross_pl, "gross_pl")
    costs.total()
    return gross - sum(
        (dec(getattr(costs, key), key) for key in allowed - included_set),
        Decimal("0")
    )
