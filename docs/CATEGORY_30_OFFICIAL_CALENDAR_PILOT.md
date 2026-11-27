# Category 30: free official calendar evidence pilot (BLS only)

**Research only; not a strategy/backtest execution path.** Live news filtering,
broker credentials, order controls and previous regression tests are untouched.

## Why

The FMP historical economic-calendar endpoint requires paid access. Historical
Forex Factory displays do not provide a licensed automated bulk history API.
For the initial replacement experiment we use the U.S. Bureau of Labor
Statistics' own annual release schedules (public domain).

## Where the source comes from

- `https://www.bls.gov/schedule/2023/home.htm`
- `https://www.bls.gov/schedule/2024/home.htm`
- `https://www.bls.gov/schedule/2025/home.htm`
- `https://www.bls.gov/schedule/2026/home.htm`

Official BLS reuse notice: `https://www.bls.gov/opub/copyright-information.htm`.
These schedules publish local U.S. Eastern times, **not UTC**. Code converts
using the IANA `America/New_York` zone, including DST. It never invents a time.

## How to collect and where evidence goes

Run the isolated workflow `category30-official-calendar-pilot.yml` on branch
`research/category30-official-calendar-pilot-2026-11-27`. It requires no
third-party API key and performs no broker requests or writes.

1. Run offline parsing/UTC/fail-closed regression tests.
2. Obtain the frozen Category 30 signal artifacts (run `45645175399`).
3. Fetch each BLS annual HTML archive **once**, with bounded size, timeout and
   no redirect/retry. If unavailable, the workflow fails; it never infers that
   an event-free period is safe.
4. Parse scheduled releases, preserve their names and convert timestamps to UTC.
5. Record source URL, raw SHA-256, raw HTML snapshot, and BLS events in each of
   the 16 frozen candidate time windows.
6. Store `report.json` and `snapshots/bls-YYYY.html` together in one **GitHub
   Actions artifact** named `category30-official-bls-<run-id>` (90 days).

After substantive verification and licensing checks for all agencies, a
versioned normalized dataset can be committed under a future
`data/historical/economic_calendar/` directory. These provisional artifacts
are **not yet** accepted as the complete retained calendar dataset.

## Important limitations and blockers

- BLS is a **subset of USD economic releases**, not complete USD/EUR coverage.
- BLS supplies event schedules, **not Forex Factory high/medium/low ratings**.
  The watchlist field is an internal research aid, not a sourced impact label.
- Archived schedules may reflect later revisions. They do **not** prove what
  the bot would have known on the historical decision date.
- Add Fed FOMC, BEA GDP/PCE, ECB rates and Eurostat release schedules, then
  separately attest coverage, timing and source provenance.
- Historical broker tradeability, costs and fills are still unverified.
- No net-P&L backtest may be authorized by this pilot.

The existing FMP code/workflow is kept unchanged pending the independent,
verified migration; no test is deleted to make a provider look successful.
