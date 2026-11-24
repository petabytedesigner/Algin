# Kategoria 29 — OANDA Practice historical M15 MBA data

Status: GET-only collector and immutable validated local dataset format; offline
fault-injection tests. No trading, no broker writes, no claim of profitability.

## Source and reproducible scope

Official OANDA REST v20:
GET /v3/accounts/{accountID}/instruments/{instrument}/candles

- price=MBA (mid/bid/ask), granularity=M15, smooth=false;
- explicit UTC from and to, includeFirst=true, **no count** when both
  endpoints are supplied; at most 4,000 M15 intervals per request, beneath the
  documented 5,000 candle limit.
- The initial research window proposed for EUR_USD is 2023-01-01T00:00:00Z
  through 2026-10-31T00:00:00Z (the end is exclusive).
- Uses existing OANDA Practice credentials only for authenticated **GET**
  requests. The operational DataHandler and strategy scanner are unchanged.
  The collector is only invoked explicitly; it is not run automatically.

## Evidence rules

- Every response must identify the requested instrument and M15 granularity;
  complete bars only, strict increasing quarter-hour UTC starts, finite positive
  MID/BID/ASK OHLC with valid price bounds, executable bid strictly below ask
  at open and close, and nonnegative volume.
- No duplicate bars or out-of-window overlap may be sorted away or deduplicated.
- Absence of candles during the broad FX weekend closure is recorded as
  WEEKEND_CLOSED, never filled with invented prices. A missing candle in an
  otherwise open market is UNVERIFIED_MARKET_OPEN_GAP: the entire candidate
  dataset is rejected for replay. Holidays, broker maintenance, trading halts
  and instrument-specific schedules require independent evidence; the broad
  weekly calendar is not proof of individual instrument availability.
- Input windows and returned source are checked again when loading the dataset.
  The saved JSON is canonical and content-addressed by SHA-256, fsynced before
  publication, and never overwritten. It contains no API token/account ID or
  raw broker HTTP headers.
- This is verified **source shape and time coverage**, not a broker-quality
  guarantee for fills, real liquidity, slippage, financing, commissions or
  historical account tradeability. Historical news/conversion inputs also
  remain unverified; Category 30 must fail closed when these are absent.

## Operator command (not executed by CI)

Run explicitly in a configured OANDA Practice environment after confirming
storage capacity; this downloads historical candles but sends no orders:

    python -m data.historical_oanda --instrument EUR_USD \
      --start 2023-01-01T00:00:00Z \
      --end 2026-10-31T00:00:00Z \
      --out .local/historical

The output reports the saved path, checksum, number of candles, paginated
requests and classified weekend gaps. A failed/missing/inconsistent response
does not produce a certified dataset. Store the generated file outside GitHub
and back it up securely. A historical dataset can still be incomplete for
economic-calendar, conversion and spread/slippage evidence even when its
candles pass these checks.

## Test verification

python -m unittest discover -s tests -q

Dedicated Category 29 tests use only synthetic OANDA-shaped JSON and mock GET
responses to verify: pagination, query parameters, strict price validation,
in-week missing bars, weekend gaps, duplicate/out-of-window rejection,
immutable SHA-256 storage, tampering rejection and credential guards.

No actual OANDA Practice/Live GET was performed as part of the offline tests.
The broker-write gates remain independently closed. Category 30 is the
separate historically constrained replay and evaluation stage.
