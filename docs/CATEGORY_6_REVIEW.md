# Category 6 — Observability, final tests and performance analytics

Scope: P57 P58 P59 P60 P61 P64 P65 P66 P67 P70 P71 P74.

- CSV events have explicit attempt/success/failure/unknown outcomes and stable correlation IDs. The append-only CSV is not authoritative broker state.
- CSV_LOG_SIGNALS/NEWS_BLOCK/SESSION_BLOCK/COOLDOWN are applied in the read-only scan. Other CSV_LOG_* flags remain reserved for real broker events, which are not emitted in OBSERVE_ONLY mode.
- Every scan branch is assigned a reason code; no trading credentials, raw requests or response payloads are serialized.
- Heartbeat records ACTIVE, CYCLE_COMPLETE and BLOCKED with atomic fsynced replacement. It does not grant permission to trade.
- P64–P67 require regression of actual fake-broker LONG/SHORT, rejection, lost reply, restart, deduplication and partial-close paths. Categories 3–4 contain synthetic tests for those behaviors; all are rerun by Category 6 CI.
- P70 OHLC same-bar SL/TP ambiguity assumes SL first for both LONG/SHORT; gap fills and intrabar order are not inferred.
- P71 cost breakdown includes spread, slippage, commission and financing, with caller-attested inclusion in gross P/L to prevent double deduction. Gross broker realized P/L must not be assumed to exclude spread.
- P74 performance requires per-trade realized NET P/L and instrument/session attribution. Missing trades, duplicate trade IDs, or unknown net costs are not silently estimated.
- No live OANDA broker writes or performance claims. The bot remains OBSERVE_ONLY and an external economic-calendar provider is still absent.
