# Algin: six-category staged engineering plan

The baseline is \`petabytedesigner/Algin\` at commit \`0f4ad93ed01ff2107b7926cdcfd6190e2018f520\`. The 63 requested code-actionable items follow the original conversation numbering (P1–P75, excluding 12 externally dependent items).

| Category | P-items | Implemented building blocks | Acceptance condition / work that remains |
|---|---|---|---|
| 1. Foundation, naming, interfaces | 1, 2, 4, 6, 7, 8, 63 | Normalized \`main.py\`, \`config.py\`, standard module names, environment-only secrets, UTC timestamps, CI, new contracts | Remove/retire all old unused helper paths in measured patches; clean install and integration run must work |
| 2. Risk and position plan | 5, 9–18 | Risk decisions separated from Decimal sizing; per-grade, total risk, instrument precision and margin reserve, existing-trade accounting | Broker-side trade sizes, actual margins and account conversion factors verified in Practice; unknown risk tracked |
| 3. Broker submit safety | 19–24, 26–30 | OANDA v20 Practice client, priceBound, explicit bid/ask, fresh quotes, journal, no mutating retry, trade ID validation, protection readback, OPEN_ONLY | Fully exercised timeout/HTTP rejection/partial-fill matrix with broker fixtures and eventual Practice tests |
| 4. State and trade management | 31–40 | Atomic account-bound JSON state, exclusive process lock, openTrades/pendingOrders snapshots, intent reconciliation, signed R, BE and partial-close once | Real event-consistent broker reconciliation; closure P/L and manual intervention scenarios, broker-side mutations in Practice |
| 5. Strategy and data quality | 41–47, 50–52, 54, 56 | Completed chronological candles, prior breakout/retest zone, M15 signal de-duplication, DST-aware entry windows, fail-closed news snapshot cache, base EMA/ATR kept | Full BOS/CHoCH causal verification, directional scoring calibration, controlled historical replay; tune only after evidence |
| 6. Audit, tests and measurement | 57–61, 64–67, 70, 71, 74 | New single-schema CSV, heartbeat phases, offline broker scenarios, cost-aware P/L analysis, conservative same-bar exit | Verified end-to-end mock order lifecycle, automated audit for all 63 work items, out-of-sample data and forward test |

**Do not mark an entire category COMPLETE because one feature compiles.** Each P-item requires a targeted test and a safety review. The statuses above reflect real delivered code, not a blanket claim of 63 completed tasks.

## Externally dependent items — not conclusively complete

P3: actual account secrets and API environment; P25: live spread measurement; P48/49: score and instrument calibration; P53: instrument entitlements on the account; P55: external economic-news provider; P62: Android/Termux runtime/keepalive; P68: actual OANDA Practice order to closure; P69: historical market dataset; P72/73: controlled strategy comparisons and out-of-sample validation; P75: forward test.

## Deployment gates
1. GitHub Actions offline suite passes at the exact commit and no secrets are in Git.
2. Account/instrument and news provider match the intended Practice environment.
3. No UNKNOWN/UNVERIFIED intents or unknown-owned exposure is present.
4. Risk, margin, priceBound, spread and account conversion all pass on current broker data.
5. OANDA Practice end-to-end opening, SL/TP verification, BE, partial, close and restart recovery demonstrated in controlled conditions. Until then, default to observation only.
6. Strategy backtest includes costs, conservative intrabar ambiguity, multiple regimes and holdout results; positive expected value is **not guaranteed**.

## Git and test policy
Preserve main baseline. Develop on isolated branch. Each change has one observable purpose and CI verification. Never access the unrelated Alvin repository. logTX remains read-only.
