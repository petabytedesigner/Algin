# Category 30 — candidate-only historical price paths (exploratory)

**Scope:** run exactly the 16 previously frozen strategy candidate decisions
against the separately attested Category 29 EUR_USD M15 bid/ask candles. This
is **not** the production `analytics.strategy_replay.replay` backtest, is not a
calibrated broker fill simulation and does not estimate net profitability.

## Historical inputs and provenance

- Pinned 95,320 M15 source bars: payload SHA
  `b48e0f98a2fe948d83afd1d384ae8b23e28b04c1275425c06b543cf75dce5466`.
- Exactly 46 monthly reports and 16 unique candidate setup IDs from GitHub
  Actions run `45645175399` (Stage 1), validated by
  `analytics.historical_evidence_gate.inspect_signals`.
- Explicit source/manifest and time coverage validated before reading any
  candidate price path. No orders, trading requests or broker writes.

## Hypothetical scenario, explicitly NOT an executable trade

1. Use the next M15 bar's sampled historical **ask open** for LONG, **bid
   open** for SHORT. Require a contiguous next bar; block if its spread exceeds
   Algin's production `MAX_SPREAD_PIPS` cap.
2. Calculate ATR14 using **only candles closed before** that entry bar. The
   research stop uses `max(ATR_STOP_MULTIPLE * ATR14, MIN_DISTANCE_PIPS)` and
   the existing `RISK_REWARD_RATIO` from `config.py`, preserving the current
   M15 strategy settings. No separate structure-level stop is available in
   these candidate artifacts; therefore this is the isolated ATR-only case.
3. Fix stop and target at planning time, then apply adverse assumed entry
   slippage (0, 1 or 2 pips). Opposing bid/ask paths are used for exits, so
   sampled spread is already present and is **not double-counted**.
4. A stop/target touched within the same OHLC candle resolves to **STOP**.
   A gap through stop uses the less favorable open. Exit slippage is applied
   adversely to stop exits. Target fills are capped at the intended level.
5. An unobserved interval during a price path is marked **UNRESOLVED**;
   no imagined closing price, forced win or forced loss is inserted. An open
   candidate at the end of available history remains unresolved.
6. Outcomes are hypothetical **gross price-path R**, before commissions,
   financing and actual broker execution effects. They are calculated per
   independent candidate, **not** as a continuous account or portfolio.

Not modelled: news approval, historical per-account tradeability, margin and
currency-conversion facts, concurrent positions, risk reservation, cooldown,
protective-order acknowledgement, swaps or actual fills. Do not promote the
output into broker-verified trade count, realized win rate, or net P/L. This
report does **not** weaken the production fail-closed replay engine.

The historical news contract and conversions remain missing as recorded in
`docs/CATEGORY_30_EVIDENCE_GATE.md`. FMP publishes an economic-calendar API
with a 90-day maximum request window and subscription-dependent date access,
but it has **not** been demonstrated that the required 2023–2026 historical
event coverage can be authenticated: https://site.financialmodelingprep.com/developer/docs/stable/economics-calendar.

## Reporting decision

Publish outcomes separately for adverse slippage 0, 1 and 2 pips; retain
unresolved counts as a distinct class. State prominently that the entire
16-candidate source and its 7 candidates in the frozen 2026 holdout are
insufficient to establish a robust profitability estimate. Only a later
properly evidenced full replay can produce operationally relevant statistics.
