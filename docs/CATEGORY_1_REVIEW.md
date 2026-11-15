# Category 1 — review before integration into main

Baseline `main`: `0f4ad93ed01ff2107b7926cdcfd6190e2018f520`.
Branch: `development/category-1-foundation-2026-11-15`.
Scope: P1, P2, P4, P6, P7, P8, P63 only.

## Acceptance matrix

| Point | Outcome | Offline evidence |
|---|---|---|
| P1 | Baseline retained, isolated history | SHA and diff/main review |
| P2 | Install manifest, `--check`, CI | pip/compileall/unittest/check; real account still untested |
| P4 | `RiskEngine.evaluate_candidate` wired | mock candidate/cooldown tests |
| P6 | `TradeExecutor` normalized read-only methods; no writes | broker fixture, invalid snapshot and no-network tests |
| P7 | LONG/SHORT validation + signed units | positive/negative direction tests |
| P8 | Strict timezone-aware UTC / cooldown | offset, naive timestamp and expiry tests |
| P63 | Canonical filenames/classes, remove generated artifacts, purge dead writes and duplicate CSV | import/AST-like code assertions and CI |

## Design boundaries and safety

- No order is sent; `open_market_order`, `modify_sl_tp`, `close_trade` and legacy `PartialCloseEngine.check_and_execute` fail explicitly.
- No unreachable POST/PUT implementation remains in the executor and shared HTTP helper. The helper accepts GET only.
- `SafetyCheck` uses the same normalized broker trade snapshot and returns a broker-error report on unavailable/malformed data.
- The broker GET endpoints and field names follow official OANDA REST v20; no real Practice request has been performed.
- The existing strategy, risk numeric defaults, and initial instrument list were not retuned.
- The original tracked state, CSV and compiled artifacts are absent from this branch but preserved in baseline history.
- The legacy economic-news and trade-management policies are not activated. State migration is deferred to Category 4.

## What passing CI does NOT establish

- It does not verify OANDA Practice access, account permissions, real fills, or the resulting trade lifecycle.
- It does not demonstrate reproducible full system operation on Termux.
- It does not establish risk-profile consistency, profitable performance, or production readiness.
- It does not mean the later five categories have been implemented.

## Merge gate

1. Confirm the branch SHA is the tested SHA and `main` is still the expected baseline.
2. CI must pass compileall, all category-1 tests and `main.py --check`.
3. Review all changes in the diff; reject any order activation or accidental strategy/risk retuning.
4. Obtain explicit user approval before merging.
5. After merge, use the new main SHA as the foundation for the next isolated category branch.
