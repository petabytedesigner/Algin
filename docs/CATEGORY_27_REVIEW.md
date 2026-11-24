# Kategoria 27 — Advanced Debugging & Decision Trace

Status: **implemented, offline-tested; no OANDA broker writes**.

## What was missing

The existing `events.csv` records event/reason codes and correlation IDs but
cannot explain the numeric EMA/ATR/price or scoring threshold used for a given
accepted/rejected strategy observation. The authoritative broker evidence is
already separate (`IntentJournal`, lifecycle state, and `AuditProjector`); a
signal observation must **never** be mistaken for a broker fill, risk approval,
or confirmed account state.

## Isolated implementation

- `storage/decision_trace.py` appends one explicitly allowlisted record for
  each LONG and SHORT evaluation of a **new completed M15 candle** to
  `csv/decision_trace.jsonl` (or configured `CSV_PATH`). It captures UTC
  cycle/candle times, instrument, direction, strategy version, EMA20/50, ATR,
  close/bid/ask, available scoring metrics, configured numeric thresholds,
  score, grade, and exact rejection reason or `OBSERVE_ONLY`.
- An observed signal has a deterministic `signal_id` compatible with the
  coordinator's existing derivation, plus the validated `setup_id`. Rejections
  record their reason and whatever numeric evaluation reached the scoring
  stage. Scans blocked **before strategy evaluation** remain in `events.csv`
  only; they do not claim nonexistent signal evidence.
- `risk_status` is always `NOT_EVALUATED_UNRECONCILED`. Broker attempt/trade/
  transaction IDs are **always null** here; authoritative broker correlation
  requires verified intent, lifecycle, and transaction evidence separately.
- Append has an exclusive writer lock, full-record `fsync`, checksum chaining,
  strict schema validation, corruption/torn-tail detection, exact per-cycle
  identity and conflict detection, symlink refusal, and a 64 MiB size cap.
  Full trace verification is performed on append. Any trace failure blocks
  the scan with `AUDIT_FAILURE`, not a silent best-effort skip.
- Fields are strictly allowlisted and numeric where appropriate. Broker
  responses, auth tokens, environment variables, arbitrary strategy text,
  and account secrets cannot be placed in the trace as extra fields.
- The existing CSV schema, strategy decision rules, scoring, risk decisions,
  broker order payloads, and state/journal formats are unchanged.

## Verification and operational limits

Command: `python -m unittest discover -s tests -q`.

Synthetic Category-27 tests cover read/write, repeat/conflicting identities,
unknown keys/NaN/secret-bearing values, denied unverified transaction IDs,
truncation, tampering, symlink/competing lock, deterministic signal links,
and read-only scanning plus `AUDIT_FAILURE` when the trace write fails.

- This is **integrity checking, not cryptographic authenticity**. A party able
  to rewrite the entire JSONL file can recompute unkeyed checksums. Use proper
  filesystem access controls and external backups.
- The file **is not rotated automatically**: reaching 64 MiB blocks new scans
  until controlled archival/rotation is implemented in Category 28. Existing
  data must never be silently truncated or deleted to recover operation.
- On each append, existing evidence is reverified, with cost proportional to
  log size. Category 28 should introduce verified segment archival before
  sustained production use; it must preserve evidence continuity.
- The trace does not capture tick-level market data, rejected pre-strategy
  gates, completed risk sizing, or verified order lifecycle. Those are covered
  by existing audit sources or planned historical-data/archival categories.
- The M15 candle checkpoint may already be claimed when a decision trace
  fails. This blocks the current scan; subsequent attempts do not fabricate
  a duplicate decision for that candle. Supervision and explicit incident
  recovery are required.
- Profitability, real-world broker reliability and backtest validity are not
  established by these offline diagnostics. Broker writes remain disabled.
