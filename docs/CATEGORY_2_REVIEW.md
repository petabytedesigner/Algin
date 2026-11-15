# Category 2 — Risk and trade plan (P5, P9–P18)

Branch: `development/category-2-risk-trade-planning-2026-11-15`.
Parent `main` SHA: `523e04ba31b3bc51098cf2b573f34c805b54cee1`.

## Rules and default values

- Grade A: **1.0%** risk budget; Grade B: **0.5%**.
- Total reserved risk **1.0%**; no independent escalation of the original total cap.
- Maximum open/reserved intentions 3, with previous grade-specific caps (A 2, B 3).
- One position per instrument; hedging disabled.
- Conservative equity base = lower of account balance and NAV.
- ATR stop multiple 1.5 and minimum 3 broker-defined pips, with reward/risk >= 3.
- Maximum modeled margin consumption = 50% of available margin.
- Entry lock after 3 consecutive losses or 5% drawdown from verified NAV peak.
- All numbers above are provisional **risk policy**, explicitly configured in `config.py`; not a profitability claim.

## Exact coverage of category points

| Item | Implementation | Limitation / verification boundary |
|---|---|---|
| P5 | `main.build_trade_plan` maps signal, ATR, context to a preview-only `TradePlan` | No order submission |
| P9 | Risk-per-grade separate from total reserved risk | Policy must be approved for production |
| P10 | Decimal-only positive finite inputs, validated configurations | No external untrusted input treated as safe |
| P11 | Read-only full OANDA Account details; balance/NAV/currency/margin and count checks | No live OANDA credentials in CI |
| P12 | `includeHomeConversions=true`, quote `accountLoss`, base `positionValue` | Missing conversions block; discontinued quote factors ignored |
| P13 | Signed LONG/SHORT stops and targets using broker quote and ATR, outward rounding | Fill slippage/gaps and guaranteed-stop restrictions are Category 3 |
| P14 | Broker instrument pipLocation, displayPrecision, tradeUnitsPrecision, min/max limits | CFDs with unverified contract multiplier blocked |
| P15 | Round unit size *down*, then recalculate monetary loss and required margin | Margin remains an estimate; broker final authority |
| P16 | Verified reservation book combines OPEN/PENDING/OUTCOME_UNKNOWN | Live journal and atomic recovery are Category 4; none are assumed absent |
| P17 | Total count, grade count, concentration, duplicate instrument and hedging blocked | Portfolio correlation beyond same symbol not inferred |
| P18 | Loss-streak, drawdown, instrument cooldown entry locks | Existing-position *management* belongs to Category 4; entry lock never orders liquidation |

## OANDA REST v20 source contracts

- `GET /v3/accounts/{accountID}`: account identity, NAV, balance, margin, open trade and pending order inventories.
  https://developer.oanda.com/rest-live-v20/account-ep/
- `GET /v3/accounts/{accountID}/instruments`: instrument pip, precision, limits, margin rate.
  https://developer.oanda.com/rest-live-v20/primitives-df/
- `GET /v3/accounts/{accountID}/pricing?includeHomeConversions=true`: prices with separate accountLoss and positionValue conversion factors.
  https://developer.oanda.com/rest-live-v20/pricing-ep/
  https://developer.oanda.com/rest-live-v20/pricing-df/
- Per-price `quoteHomeConversionFactors` is deprecated and **not used** for sizing.
- Sample OANDA instrument data use `maximumPositionSize: "0"` when no position limit is specified; implementation treats 0 as no additional instrument-level cap but still enforces maximumOrderUnits and the portfolio risk/margin rules.

## Integrity / release gates

The account, instruments and pricing are three **distinct GETs**: a successful set
does not prove an atomic broker snapshot. This category refuses broker writes.
Exposure and loss state must be **independently verified** and explicitly supplied
by a future reconciliation component; tests use synthetic verified fixtures.
No runtime path fabricates `verified=True` or assumes no ambiguous requests.

After reviewing CI, only user approval allows merging this entire category.
Do not describe a unit plan as an executable order or guaranteed worst-case loss.
