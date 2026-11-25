# Category 30 — Stage 4 historical-news source feasibility

**Purpose:** prepare an evidence acquisition route for the 16 immutable
EUR_USD M15 research signal candidates. This stage is **not** a completed
backtest, historical broker attestation or proof of profitability.

## Exact scope

1. Read all 46 monthly files from frozen Category 30 Stage 1 run
   `45645175399` and verify them through the Stage 2 evidence gate, including
   immutable source digest and all 16 candidate identities.
2. Deduplicate signal decision dates in UTC and prepare a query covering
   **the day before, the decision day and the day after** each decision. The
   provider's documented maximum single request is 90 days; each planned
   request here is only three days. At most 16 queries are planned.
3. The original GitHub `push` first generated the plan only. For a controlled
   source-feasibility check, subsequent `push` runs now probe **at most the
   earliest single decision window** using a preconfigured FMP key; without
   it, the report marks a blocker and sends no request. On an **explicit
   manual workflow dispatch** with `probe_fmp=true`, attempt one bounded GET
   for each date window using a
   preconfigured `FMP_API_KEY` GitHub Actions secret. Never print an API key,
   response URL, authentication details or raw provider errors.
4. Validate every returned event (UTC timestamp, currency, impact, title,
   date-window bounds, duplicates, payload size) without dropping malformed
   rows. Record source-response SHA-256 and sanitized EUR/USD-relevant events.
   A missing key, HTTP error, empty/invalid response or gap stops the probe;
   never retry ambiguously or treat a failed request as a no-news day.
5. Save a report artifact with exact requested windows and explicit blockers.
   A retrospective provider response is **only a source sample**. It does
   not establish that all historic events are present or that an archived
   calendar was available unchanged at the time of each 2023–2026 decision.

## Boundaries and remaining requirements

- The historical news provider is separate from OANDA; no OANDA token is
  used to authenticate FMP. The stage accesses no broker endpoints or orders.
- Neither a present-day FMP response nor OANDA M15 candle history proves
  historic per-account tradability, intrabar executable fills, historical
  liquidity, commissions, financing, conversion factors or net returns.
- The production `analytics/strategy_replay.py`, `data/news_filter.py`,
  signal thresholds and risk controls remain **unchanged**.
- The Stage 4 report **always** sets `net_pnl_backtest_permitted=false`.
  The source probe is intended to decide what further provider/evidence
  integration is possible before authorizing any full historical replay.
- `main` remains untouched. The workflow executes on the isolated research
  branch and stores its results as a GitHub Actions artifact.

## Official source contract

- [FMP historical/forward economic calendar API](https://site.financialmodelingprep.com/developer/docs/stable/economics-calendar):
  UTC event timestamps; `from`/`to` limited access; max 90-day request range.
- [OANDA pricing endpoint](https://developer.oanda.com/rest-live-v20/pricing-ep/):
  current pricing status and home conversion information; not a complete
  historical per-bar broker tradeability archive.

## Acceptance

Nine focused offline Stage 4 tests, the Stage 2 evidence guard tests and the
provider-query-plan generation must pass in GitHub Actions. The initial
automated run reported `NOT_PROBED`; the limited follow-up push either returns
`BLOCKED_NO_FMP_API_KEY`, a provider error or
`PARTIAL_RETROSPECTIVE_SOURCE_SAMPLE`, with no inferred news clearance.
A full-window manual FMP probe may be used only after separate credential
and historical access verification.
