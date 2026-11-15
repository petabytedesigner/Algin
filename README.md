# Algin — Category 1 foundation (development only)

This isolated branch is based on the **unchanged** original `main` commit
`0f4ad93ed01ff2107b7926cdcfd6190e2018f520`.
Its scope is **P1, P2, P4, P6, P7, P8 and P63**.

**BROKER WRITES ARE DISABLED.** OANDA Practice is the only configured
endpoint. The old `safe_request` rejects all non-GET operations, the
executor refuses every order/modify/close method, and `main.py` performs
only advisory scanning. This is not a deployable or profitable trading bot.

## Clean installation (Python 3.10+)

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python main.py --check
python -m unittest discover -s tests -v
```

For **read-only** OANDA Practice observations, export
`OANDA_API_TOKEN` and `OANDA_ACCOUNT_ID` locally and run:

```sh
python main.py --once
```

The example env file documents variable names only. The application does
not automatically load `.env`; never commit credentials. Missing, corrupt
or ambiguous broker exposure blocks risk assessments.

## Category 1 changes

- P1: source provenance, isolated branch and clean baseline.
- P2: dependency manifest, runnable `--check` mode, offline CI tests.
- P4: `TradeCandidate → RiskEngine.evaluate_candidate → RiskDecision`
  rather than the missing legacy `evaluate_signal` method.
- P6: normalized read-only `TradeExecutor.get_open_trades/get_price` contract;
  legacy partial-close remains disabled pending Category 4.
- P7: LONG/SHORT internal labels and signed-unit validation; BUY/SELL legacy
  inputs accepted by validation only; no silent sign flipping by an active executor.
- P8: timezone-aware UTC, strict parsing, explicit opt-in for legacy naive
  timestamps, fixed cooldown comparison.
- P63: canonical module and class names, updated imports, untracked generated
  runtime artifacts, no modification to the original version on `main`.

## Deliberate deferred work

**Category 2:** approve the final risk profile, account exposure and units.
Original `RISK_PER_TRADE=1.0` and other limits are NOT harmonized here.
**Category 3:** broker POST/PUT transaction journal, price and fill verification.
**Category 4:** state recovery, reconciliation, BE, partial close.
**Category 5:** completed candle validation, strategy fixes and news feed.
**Category 6:** standardized events/CSV, audit and performance testing.

Duplicate per-module CSV writers and unreachable broker-mutation code were
removed from the executor, safety check and legacy partial-close module. The
original trading strategy remains unchanged and is observation-only here.
The CSV event schema and actual BE/partial-close policy belong to Categories
6 and 4 respectively. Actual OANDA Practice
order execution and Termux runtime are not validated. The earlier mixed
development branch was inspected, **not merged or copied wholesale**.

Do not merge this branch until its offline checks pass and the design decision
to accept an observation-only intermediate baseline is reviewed.

## Merge review

This is a **read-only interim baseline**. Its merge into `main` will replace
the previous order-submitting loop with an observation-only entrypoint.
It is not a trading-ready release. Do not merge without explicit approval.
Review `docs/CATEGORY_1_REVIEW.md` and the latest CI result first.
