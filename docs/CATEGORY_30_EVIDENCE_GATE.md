# Category 30 — historical evidence readiness, research only

**Decision on 2026-11-25:** price-time coverage and signal-candidate lineage
are verified for the frozen EUR_USD M15 research window; **realistic net P/L
backtesting is NOT YET READY**. No strategy rules, execution gates or
production code have been changed by this stage.

## Frozen input lineage

1. Category 29 price research artifact from GitHub run `45617330337`,
   verified payload SHA-256
   `b48e0f98a2fe948d83afd1d384ae8b23e28b04c1275425c06b543cf75dce5466`,
   95,320 M/B/A M15 source candles, January 3 2023 through October 31 2026
   exclusive. The coverage manifest explicitly states that fills have **not**
   been verified.
2. Category 30 signal diagnostic from GitHub run `45645175399`, four year
   artifacts and 46 distinct monthly JSON files. Strict checks require each
   file to reference the same price SHA, retain the research-only/unknown
   status and report zero executed trades and no computed profitability.
3. Exactly 16 unique signal candidates (2023=2, 2024=6, 2025=1,
   2026 through October=7), all Grade B. These are **before** independently
   verified historical economic-calendar or broker-tradeability decisions.

The `analytics.historical_evidence_gate` checker fails on an altered/missing
month, checksum mismatch, contradictory claims, duplicated candidate identity,
future timestamps, altered baseline or missing manifest. Its output includes
the digest of every monthly JSON, the manifest digest and an explicit list of
unresolved evidence requirements. It cannot emit or simulate an order.

## Evidence available versus not yet attested

| Input | Current evidence | Important boundary |
| --- | --- | --- |
| Historic UTC M15 mid/bid/ask OHLC | Category 29 exact source SHA and bounded coverage | OHLC is **not** tick chronology or a fill quote |
| Historic spread from M15 bid/ask | Observable at sampled OHLC boundaries | Not guaranteed at next actual broker fill |
| Market opening schedules | `MarketSessions`, including timezone-aware schedules | Schedule alone is not per-account tradability |
| Historical broker tradeable status | **Missing** for each bar | Current OANDA pricing can report status, but these historical candles do not |
| Historical news/calendar | **Not attested** | FMP adapter handles current verified windows, not a proven 2023–2026 archive |
| Historical account-currency conversion | **Missing** as per-bar signed evidence | Account currency and cross-rate provenance need explicit verification |
| Commission and financing | **Not attested** for this historic account window | Zero is a scenario assumption, not a source fact |
| Order fill and slippage | **Not attested** | M15 bid/ask candle cannot certify liquidity or fill probability |

The production replay (`analytics/strategy_replay.py`) intentionally requires
complete per-bar conversions and a boolean `broker_tradeable`, and its
historical news gate fails closed. Do not satisfy these contracts by simply
marking every bar tradable or by inserting a fabricated empty calendar.
Model-based sensitivity studies may be done **later**, in a distinctly
labelled scenario with explicit non-broker assumptions; they must never be
described as broker-verified net returns.

## External primary references

- [OANDA v20 pricing endpoints](https://developer.oanda.com/rest-live-v20/pricing-ep/):
  live price objects include instrument tradability and liquidity tiers;
  candle history is a distinct sampled OHLC resource.
- [OANDA historical instrument candles](https://developer.oanda.com/rest-live-v20/instrument-ep/):
  bid/ask/mid candle requests and M15 granularity.
- [FMP economic-calendar official documentation](https://site.financialmodelingprep.com/developer/docs/stable/economics-calendar):
  `from`/`to` parameters show a maximum 90-day request window and limited
  access for dates. Documentation does not by itself establish that every
  event for 2023–2026 is accessible, covered and historically accurate.

## Next step / acceptance condition

Acquire and separately attest real historical economic-event coverage,
account-specific currency conversions and broker execution/tradability
assumptions or evidence. Preserve 2026 as a frozen out-of-sample period.
The 7 tentative 2026 candidates are insufficient to make a statistically
confident profitability claim. A later scenario study can report hypothetical
returns with sensitivity bands, but Category 30 must not silently relabel
assumptions as measured broker outcomes.

The GitHub Actions evidence report is a **readiness audit only**, not a
strategy backtest or an authorization to activate trading.
