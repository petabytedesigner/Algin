# Category 6 — Audit, regression and reliable financial reporting

Development branch: development/category-6-audit-reporting-2026-11-15
Baseline main: da636c4d7aad804ca039e1492c7df614a47c334f

Scope: **P57 P58 P59 P60 P61 P64 P65 P66 P67 P70 P71 P74** (12 points).
Other 12 external-only points remain excluded from the code integration.

## Requirements and implementation

| Point | Result / verification |
|---|---|
| P57 | Fixed 24-column CSV for attempts, confirmed fills, rejections, unknowns and closes; durable append, no silent repair |
| P58 | All CSV_LOG_* options mapped to event types; disabled flags suppress matching output |
| P59 | Fixed reason code for no signal, failed retest, low score, stale candle, risk/margin, spread, news, session and unresolved state |
| P60 | Trace from signal, candle, grade and risk to request, broker result, trade and confirmed closure via journal/lifecycle/audit IDs |
| P61 | Account-scoped atomic heartbeat distinguishes ALIVE, CYCLE_COMPLETE, BLOCKED and separate broker confidence |
| P64 | Existing Categories 1–5 regressions plus Category-6 tests all run in CI |
| P65 | Positive LONG and SHORT with fake broker, distinct order and trade IDs, confirmed SL/TP, never real broker writes |
| P66 | Margin/spread/data/exposure rejection before any POST, and recovery only after verified prerequisites |
| P67 | Timeout, UNKNOWN, restart and GET-only settlement without second POST; partial UNKNOWN remains reserved |
| P70 | If single bar touches SL and TP, choose STOP; finer bars only if full chronology and OHLC reconciles |
| P71 | Signed financing/dividend, positive commission/fees, spread/slippage with explicit executable-price or mid-price basis |
| P74 | Verified closed-trade net P/L, avg profit/loss, win rate, expectancy, PF, drawdown, losing streak, group by instrument/session, large winner concentration |

## Source trace, event schema and P57–P61

telemetry/events.py writes a single versioned account-hash CSV to CSV_PATH.
Stable event IDs prevent duplicate accounting. File locks and fsync guard
append-only writes. A broken header, partial row, symlink or stale lock is
rejected, never silently repaired. Only approved event fields can be written;
raw broker HTTP responses, token strings, stack traces and free-text errors
are never accepted. A repeated event with identical payload is idempotent. If event persistence
fails after a durable order intent is created, the broker POST is not made,
and the PREPARED journal state blocks further blind attempts.

telemetry/runtime.py wraps scan_once, manage_once and held-lease reconciliation.
Heartbeat is written ALIVE before work and CYCLE_COMPLETE only after
successful completion. BROKER OPEN_TRADES_ONLY does not mean pending orders
or unknown mutations were reconciled. The heartbeat filename includes an
account hash to prevent cross-account overwrite. Stale / ALIVE / BLOCKED
states are not proof of a healthy running process; manual review is required.

OrderEngine journals the intent before logging PREPARED and before any fake
broker POST. Later results and verified protection are recorded as separate
events. AccountReconciler reconstructs BE, partial, closure and cooldown events
from confirmed durable state on each run; this permits idempotent backfill.
There is no automatic broker order retry. Signal/risk ID correlation comes
from the durable journal and observed strategy signal IDs.

CSV_LOG_SL_HIT, CSV_LOG_TP_HIT, CSV_LOG_MANUAL_CLOSE and
CSV_LOG_SYSTEM_CLOSE are supported taxonomy flags, but events are only
emitted when a trusted broker transaction or operator source proves the
reason. Missing open exposure or mid-price OHLC cannot prove a real closure
reason, so no such event is fabricated.

## P70 conservative simulation

A single M15 OHLC bar has no known intrabar path. If both stop-loss and
take-profit are hit, STOP_FIRST is the declared conservative result. Finer
bars must be complete, strictly ordered, contiguous and cover the entire
parent M15 interval with matching OHLC before their order is used. Partial
subbar coverage never authorizes an optimistic take-profit. These are
synthetic outcomes, not evidence of actual OANDA execution price.

## P71 cost accounting

OANDA v20 exposes realizedPL, financing, commission, halfSpreadCost and
guaranteedExecutionFee separately:
https://developer.oanda.com/rest-live-v20/transaction-df/
https://developer.oanda.com/rest-live-v20/trade-df/

Every CostRecord requires explicit home-currency numeric components.
EXECUTABLE_FILLS already includes spread/slippage in executable price,
so spread/slippage values are informational and not subtracted again.
MID_REFERENCE simulated P/L subtracts those costs exactly once. Both modes
include signed financing/dividend and positive commission/GSLO fee once.
Absent components are not silently converted to zero. CostLedger prohibits
repeating a single transaction's fees across multiple trades without an
independent, trusted attribution contract.

## P74 performance limits

The report requires unique completed trades, complete cost transaction lists,
initial equity and chronologically ordered closure evidence. Unexplained
costs or a missing trade cause a hard error. Profit factor is undefined
(None) with no losses; a large-winner concentration flag identifies excessive
reliance on a few gains. Session metadata is derived and verified from the original opened_at
UTC timestamp (IANA DST-aware FX sessions). Missing or conflicting times
are rejected before any session-level performance result is produced.

## Operational caveats

All tests use synthetic broker/candle/transaction data. Real OANDA Practice
POST/PUT/PATCH remains disabled by an independent Category-3 release gate.
The main application remains OBSERVE_ONLY. No real fills, profitability,
commission attribution or production restart have been validated here.
News-calendar P55 remains an external provider task and NEWS_UNKNOWN blocks
entry eligibility. LogTX was read-only reference evidence and was not modified.

Verification:
    python -m compileall -q .
    python -m unittest discover -s tests -v
    python main.py --check

Merge only after complete GitHub Actions pass, explicit user authorization,
and verification that the final main tree equals the tested branch tree.
