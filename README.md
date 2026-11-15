# Algin — development baseline: Categories 1–6

Categories 1 and 2 implement the foundation, read-only broker contracts and
pure risk-based trade preparation. The active entrypoint remains **OBSERVE ONLY**.
No order placement, amendment or closing is enabled. A trade plan is not a
broker instruction, and risk computed from a stop does not guarantee the
maximum realized loss in markets with gaps, slippage or extra fees.

## Python 3.10+ setup

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check
python -m unittest discover -s tests -v
```

For a read-only OANDA Practice observation, locally export `OANDA_API_TOKEN`
and `OANDA_ACCOUNT_ID`, then `python main.py --once`.
Secrets are never stored in Git. `.env.example` is documentation only.

## Category 2 risk policy (provisional)

Grade A 1.0%, Grade B 0.5%; total reserved exposure 1.0% of conservative
NAV/balance; at most 3 open/pending intents and no duplicate/hedged
instrument. Stop 1.5×ATR or at least 3 OANDA pips, TP >= 3R. Margin cap
50% of available margin. Lock new entries after 3 consecutive losses, 2% verified daily realized
loss, or 5% drawdown. A configurable shared-currency risk cap limits
correlated USD/JPY/etc. exposure. Broker-external structure support/resistance
may widen ATR stops; an unverified structure level is never invented. These explicit values are in `config.py` and can be reviewed
before deployment; they are not changed automatically by grade/scoring.

`risk/trade_plan.py` is a pure planning engine with Decimal arithmetic,
LONG/SHORT signed units, broker-defined pip/price/unit precision, conservative
currency-loss conversion and post-quantization revalidation. A plan is allowed
only with a caller-verified exposure reservation book, including unresolved
order outcomes, plus a caller-verified loss history. No real account read
automatically asserts this verification.

`broker/risk_data.py` reads OANDA Practice full account details, instrument
metadata and pricing with modern `homeConversions`. Broker GET failures,
missing conversions, unrecognized instrument contract types and inconsistent
trade/order counts reject preparation. No production-ready margin/fill
assumptions are made.

See [Category 2 review](docs/CATEGORY_2_REVIEW.md) for P5/P9–P18, test
expectations and constraints. [Category 1 review](docs/CATEGORY_1_REVIEW.md)
documents the prior integrated baseline.

## Work remaining

Category 3 must implement broker mutation with write-ahead intent and
unambiguous response handling; Category 4 must implement durable
reconciliation, open-position management and authenticated loss statistics.
Category 5 addresses strategy/data quality; Category 6 event reporting,
regression and performance analysis. Real OANDA Practice integration and
Termux behavior remain untested.

## Category 3 development — no broker writes

The third category adds an isolated broker execution boundary with durable
intent journaling, direction-aware priceBound, strict OANDA v20 result decoding,
GET-only reconciliation after uncertain outcomes, and postfill SL/TP/ownership
verification. Both independent release and configuration gates block actual
broker POST requests. The active main.py remains observation-only.
Read docs/CATEGORY_3_REVIEW.md. Offline fixtures do not establish real Practice
execution, restart reconciliation for an operational account, or trade safety.

## Category 4 state and recovery (read-only OANDA)

Run `python main.py --manage-once` with local Practice credentials to perform
a full account read and conservative reconciliation of durable trading state.
This makes GET calls and writes local checksummed/atomic records to `.local/`;
it does **not** send a POST/PUT/PATCH to OANDA. The long-running main loop
holds an exclusive per-account process lease and reconciles every cycle before
the entry-session filter. An orphaned lock means inspect the previous process,
journal and broker state manually; never blindly delete state or retry a
request. Existing `--once` remains the legacy observation-only signal scan,
not the recovery command.

The lifecycle preserves original signed units, entry, original stop and risk
distance, grade, BE/partial phase states, and one-time close/cooldown accounting.
Directional R uses exit-side bid/ask and original risk. BE/partial can be
previewed and staged locally but **real management orders remain disabled**.
The initial account snapshot must match the same-account fill journal and
broker trade ownership, or management rejects the trade. Older/incomplete
trades are not adopted by guessing missing data. See
[Category 4 review](docs/CATEGORY_4_REVIEW.md).

Passing offline broker-fixture tests does not demonstrate account-wide live
recovery, real slippage, or operational readiness. The system stays
OBSERVE_ONLY pending separately authorized and verified broker-write release.

## Category 5 signal and feed-quality rules

M15 signal input must carry OANDA complete=true and ordered, nonduplicated,
finite OHLC values. The latest incomplete broker bar is excluded. A bar is
considered only after its 15-minute close. A durable per-account checkpoint
limits strategy analysis to once per completed bar, including after restart.
No signal uses a retest zone built from its own candle.

EMA20/50 + ATR remain the underlying strategy. LONG/SHORT must agree with
trend and bullish/bearish candle confirmation. The breakout requires a close
beyond a previously confirmed swing, then a subsequent retest within four
bars; an unimplemented liquidity_sweep does not lower score. Pattern quality
applies a real penalty, and an EMA reset zone blocks entry.

Session checks use IANA DST-aware London, Tokyo and New York zones with the
New York FX-week boundary. Current broker tradeable pricing and quote age are
verified independently. Without a verified news provider, P56 calendar state
is NEWS_UNKNOWN and **new entries must remain blocked**; P55 is explicitly
external. These are read-only development checks, not live trading release.

See docs/CATEGORY_5_REVIEW.md. Broker writes remain DISABLED.

## Category 6 — durable audit and offline performance analysis

The unified, account-scoped event CSV uses strict versioned columns, fixed
no-entry reason codes, idempotent event IDs and CSV_LOG_* configuration flags.
Heartbeat reports ALIVE, CYCLE_COMPLETE and BLOCKED without mistaking
openTrades-only checks for full broker reconciliation.

TradeTrace correlates signal -> risk -> intent -> broker result -> fill ->
confirmed closure. Analytics enforce conservative stop-first handling for
ambiguous OHLC and complete cost components without double-counting spread,
slippage, financing or commissions. Closed-trade reports include net P/L,
expectancy, profit factor, drawdown, streaks, instrument/session results
and large-winner concentration.

Offline integration tests cover LONG/SHORT, preflight refusals, lost broker
responses, restart reconciliation, and durable telemetry integrity. Real OANDA
Practice mutation is still hard-DISABLED and the bot remains OBSERVE_ONLY.
External news provider P55 remains unconnected. Full details and limitations
are in docs/CATEGORY_6_REVIEW.md.
