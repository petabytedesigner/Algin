# Category 5 — Strategy and market/news data integrity

Parent main commit: d47872f6f01feeff72d187d0caa94dbf9e91629e
Branch: development/category-5-signal-data-2026-11-15

Scope exactly **P41, P42, P43, P44, P45, P46, P47, P50, P51, P52, P54, P56**.
Existing EMA20/50 + ATR and break/retest family is preserved; no new profit
claim, parameter optimization or real broker order activation.

## Verification matrix

| Point | Implementation | Fail-closed behavior |
|---|---|---|
| P41 | Retest zone from swing strictly before breakout, not from signal candle | No confirmed prior pivot = no signal |
| P42 | Explicit body-close breakout then later retest/confirmation, expiry 4 bars | Old breakout, invalidated zone or same-candle retest rejected |
| P43 | Confirmed two-left/two-right swing highs/lows; BOS on cross with body close; CHoCH on opposition to prior HH/HL or LH/LL | Wick only or unconfirmed swing not a break |
| P44 | LONG needs EMA20 > EMA50 and UP; SHORT needs EMA20 < EMA50 and DOWN | Countertrend/overextended/reset blocks |
| P45 | LONG last candle must be bullish, SHORT bearish, with doji/wick rejection | Directionless/counter-candle blocks |
| P46 | Omit unmeasured liquidity_sweep from numerator AND denominator | Explicit measured zero still lowers score |
| P47 | Apply pattern_quality as a bounded multiplicative factor, including soft penalties for two near-threshold imperfections; in_reset_zone blocks signal | Invalid/NaN metrics rejected |
| P50 | Only broker complete=true M15 bars; omit only incomplete final bar | Earlier incomplete or unknown completion rejected |
| P51 | Strict UTC M15 alignment, ordered unique times, valid finite positive OHLC, recent closed bar and recent-gap check | No sorting, silent deduplication or stale-bar signals |
| P52 | Per-account/instrument M15 gate, checksum + atomic/fsynced record, claimed before strategy | Same bar/restart/corrupted gate cannot create duplicate analysis |
| P54 | IANA London/New York/Tokyo DST, New York Sunday/Friday weekly bounds, independent verified broker tradeable+fresh price gate | FX only until instrument-specific sessions validated; non-FX denied |
| P56 | Strict UTC economic calendar cache, fetched_at age/TTL, HIGH lockout by currency | Missing/stale/unrecognized source returns NEWS_UNKNOWN |

## Broker API and timezone evidence

- OANDA candlesticks use complete bool: https://developer.oanda.com/rest-live-v20/instrument-df/
- Verified account-scoped candles endpoint (OANDA help):
  https://help.oanda.com/us/en/faqs/api-user-guide.htm
- OANDA pricing currently provides tradeable boolean; status is documented
  as deprecated: https://developer.oanda.com/rest-live-v20/pricing-df/
- IANA zoneinfo (Europe/London, America/New_York, Asia/Tokyo) handles local DST.
- P55 news provider connection is EXTERNAL and OUT OF SCOPE. No
  ForexFactory endpoint or economic events are fabricated.

## Operational safety

- main.py remains OBSERVE_ONLY; Category 3 PracticeTransport's independent
  broker-write gate is preserved and deliberately unchanged.
- Signal scoring is not execution authorization. P52 checkpoint is claimed
  BEFORE strategy analysis to prioritize at-most-once over missed opportunities.
- Historical prices cannot authorize broker-side tradeability, and OANDA
  pricing must be a current tradeable quote (not stale/unknown).
- Market session schedules are not instrument trading hours: only a separate
  confirmed OANDA pricing result permits even read-only signal eligibility.
- The score thresholds, EMA20/50, ATR14 and entry risk caps are not retuned.
- Legacy tests were updated only where they relied on undocumented absence
  of complete or synthetic unordered/no-timestamp candles.
- Real Practice trading, provider news functionality, broker order execution
  and performance metrics remain unverified.
- No merge to main until PR CI passes and the user authorizes integration.

IANA timezone database is an explicit dependency (tzdata) for Windows/Termux
installations without system zone information. Historical per-candle checkpoint
and economic-calendar source invalidation are regression-tested.
