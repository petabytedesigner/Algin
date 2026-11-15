# Category 3 — OANDA execution and broker-result contracts

Branch: development/category-3-broker-execution-2026-11-15
Baseline main: 636a24106932e87b8f1620ce5604a16d5bfa3743
Scope: P19, P20, P21, P22, P23, P24, P26, P27, P28, P29, P30.

**THIS IS NOT A LIVE TRADING BOT.** The complete broker boundary is tested
with an injectable simulated transport. Actual OANDA Practice submissions
remain hard-disabled by an independent compile-time release gate plus the
configuration gate, until Category 4 reconciliation and explicit review.
The main.py observation loop does not call OrderEngine.

## Category matrix

| Point | Implementation |
|---|---|
| P19 | Single HTTP POST maximum, no retry/redirect; default transport blocks writes |
| P20 | FILLED, REJECTED, CANCELLED, UNKNOWN strictly from broker transaction evidence |
| P21 | Durable account-scoped fsync intent journal before POST, unique signal IDs, GET-only reconciliation |
| P22 | tradeID from orderFillTransaction.tradeOpened.tradeID, never orderCreateTransaction.id |
| P23 | Invalid JSON/HTTP 4xx/429/5xx/timeouts remain UNKNOWN absent definitive transaction |
| P24 | OANDA price status="tradeable", bid/ask, verified quote age, real spread limit |
| P26 | priceBound = ask + allowed slippage for LONG, bid - allowed slippage for SHORT |
| P27 | Signed units, price/size precision, stop distances, limits, and worst-bound risk |
| P28 | Store broker fill price/units and reductions/closures; unexpected changes freeze entry |
| P29 | GET opened trade, verify ownership and SL/TP, feed SafetyCheck pre-entry block |
| P30 | Identity via clientExtensions id/tag AND local journal; foreign/manual trade blocks |

## OANDA v20 contract sources

- https://developer.oanda.com/rest-live-v20/order-df/
- https://developer.oanda.com/rest-live-v20/order-ep/
- https://developer.oanda.com/rest-live-v20/pricing-ep/
- https://developer.oanda.com/rest-live-v20/trade-ep/
- https://developer.oanda.com/rest-live-v20/transaction-ep/

The documented OANDA MarketOrderRequest has signed units, FOK, OPEN_ONLY,
priceBound, stopLossOnFill, takeProfitOnFill and client/tradeClientExtensions.
The order response can include an orderFillTransaction, orderCancelTransaction
or an orderRejectTransaction; an orderCreation ID never proves an open trade.

An empty GET after a timeout, including HTTP 404, is not evidence that the
previous POST did not execute. The attempt remains UNKNOWN and is never resent.

## Safety and deferrals

- No real broker mutations, SL/TP changes or order/position closing.
- A fresh full-account GET rechecks currency, NAV/balance risk budget and
  available margin before any request is journaled.
- Plan pricing must correspond to the exact fresh broker quote and conversion.
- Configured 2-pip slippage is translated into direction-aware priceBound;
  quantization never increases size and enforces the original monetary risk cap.
- The 2.5-pip spread preflight is based on historical FX-major LogTX read-only
  checks and is conservative; other instruments require explicit verified limits.
- Broker account snapshots with a populated mt4AccountID are denied.
  A separate full account-properties check is still required before release.
- Client extensions may not be used on MT4-linked accounts. Such accounts
  must remain blocked without an alternate reviewed ownership scheme.
- Journal locks deliberately survive crashes; manual reconciliation is
  required before cleanup. Missing or corrupt state is fail-closed.
- Existing or foreign trades and pending orders block any new attempt.
- Fill confirmation never automatically repairs an unprotected trade.
- Category 4 is responsible for durable complete-state reconciliation,
  position management, restart safety and operational activation.
- Category 5 is responsible for final strategy/data quality and Category 6
  for unified event log and complete system reporting.
- All tests are offline broker fixtures; no OANDA Practice order placed.

**Do not merge this category without explicit user approval after CI/review.**
