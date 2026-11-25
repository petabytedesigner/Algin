# Category 29 — frozen EUR_USD M15 price-coverage attestation

**Scope:** historical OANDA Practice mid/bid/ask candles for research. This is
*not* an execution backtest, P&L result or a modification to the live bot.
`data/historical_oanda.py` intentionally remains unchanged and fail-closed.

## Original immutable source

- Download run: https://github.com/petabytedesigner/Algin/actions/runs/45505150800
- Historical window: `[2023-01-01T00:00:00Z, 2026-10-31T00:00:00Z)`.
- Candles: **95,418**; raw JSON SHA-256:
  `d16ad13ff69b4a4d56e9feb7a1e2cf26858da33b8a61a3abfa5a0f74707b6c6e`.
- Status: `UNVERIFIED_NOT_BACKTEST_APPROVED`. Preserve this original unchanged.
- Original coverage gaps: 198 classified weekend closures, 9 market-open
  gaps needing additional explanation. A January 2023 opening and two early
  January 2 gaps have incomplete evidence. The latter had zero M1 bars in the
  same intervals in a separate OANDA M1 probe, which is *not* tick-level proof.

## Fixed research window and excluded evidence

The **research-only** price dataset starts exactly
`2023-01-03T00:00:00Z` and ends at `2026-10-31T00:00:00Z` exclusive.
This excludes **98** source bars from the uncertain initial window, retaining
**95,320** bars. The cutoff is an evidence limitation, not a result-driven
strategy tuning step; the January 1–2 bars remain in the original archive.
No price bars are synthesized or imputed.

Six remaining gaps need an exact timestamp match before this dataset is
attested. Times below are UTC and mark missing M15 candle *start* intervals,
not exact minute-by-minute opening or tradeability claims:

| Missing interval `[start,end)` UTC | Published broker schedule |
| --- | --- |
| 2023-12-22 22:00 — 2023-12-25 22:00 | [2023 OANDA holiday notice](https://www.oanda.com/eu-en/news/holidays-in-the-following-week) |
| 2023-12-29 22:00 — 2024-01-01 22:00 | [2023 OANDA holiday notice](https://www.oanda.com/eu-en/news/holidays-in-the-following-week) |
| 2024-12-24 22:00 — 2024-12-25 22:00 | [2024 OANDA holiday notice](https://www.oanda.com/eu-en/news/trading-hours-during-christmas-and-new-year) |
| 2024-12-31 22:00 — 2025-01-01 22:00 | [2024 OANDA holiday notice](https://www.oanda.com/eu-en/news/trading-hours-during-christmas-and-new-year) |
| 2025-12-24 22:00 — 2025-12-25 22:00 | [2025 OANDA US holiday notice](https://www.oanda.com/us-en/trading/holiday-trading-hours/) |
| 2025-12-31 22:00 — 2026-01-01 22:00 | [2025 OANDA US holiday notice](https://www.oanda.com/us-en/trading/holiday-trading-hours/) |

Broker notices cover different OANDA divisions and express some FX reopening
times with a five-minute offset relative to M15 bar boundaries. They support
**holiday explanations for the observed absent candle envelopes**, not exact
verification that all minutes were unavailable on this Practice account.
Do not silently treat these bars as executable or synthesize holiday prices.

## Research-only audit control

The dedicated `category29-history-final-attestation.yml` GitHub Action:

1. Runs unit tests; downloads the original unverified artifact from the pinned
   GitHub run; hashes the raw bytes before parsing anything.
2. Revalidates every original candle, its order, OHLC and raw gap ledger.
3. Applies the fixed research cutoff, recomputes missing M15 intervals, and
   requires an **exact one-to-one match** to all six documented holiday gaps.
   Any additional gap, modified price or mismatched evidence fails the job.
4. Produces a versioned artifact with a source-linked manifest and price JSON,
   preserving the number of excluded bars, holiday citations and two hashes.

The job runs *only* on the temporary research branch; it does not fetch fresh
broker data, request or use tokens, place orders, merge to `main` or run a
strategy P&L simulation. An `attested` status means the stated **price-time
coverage** passed this procedure only.

**Still for Category 30:** historical economic events, tradability and exact
broker fills, currency conversion, financing, execution slippage and related
costs. Those missing inputs must continue to block any claims of realistic
performance, even after Category 29 price coverage is attested.
