# Live economic-calendar pilot: ShowMyTrades

Scope: a **prospective** HIGH-impact news lockout provider, not a replacement
for FMP's **historical** calendar/backtest dependency. No source is able to
guarantee that an unseen economic release cannot exist; the ShowMyTrades
coverage assertion is based on its published filtered response contract.

Provider: https://showmytrades.com/docs/api/economic-calendar

## Selection and compatibility

- The forward/live bot defaults to `NEWS_CALENDAR_PROVIDER=SHOWMYTRADES`.
  `ALGIN_NEWS_CALENDAR_PROVIDER=FMP` exists only as a deliberate compatibility
  override; FMP's historical/backtest ingestion remains separate.
- Static test news fixtures retain highest precedence. An opt-in ShowMyTrades
  setting together with a custom `NEWS_PROVIDER_URL` fails closed instead of
  choosing an ambiguous provider.
- The live GET is fixed to eight major currencies, `impact=high`, `days=1`.
  It has **no API key**, and never uses an OANDA token.
- `NewsFilter` still applies inclusive ±30-minute lockouts to every HIGH event,
  with its existing TTL, source fingerprint, coverage checks and `NEWS_UNKNOWN`
  on uncertainty. Existing replay calendar injection is unchanged.

## Evidence and failure behavior

The adapter requires HTTP 200, a bounded JSON body, a fresh UTC server clock,
`generated_at`, `from`/`to` coverage, and an exact `count` matching the event
inventory. Events require UTC ISO time equal to the Unix epoch, supported
currency, HIGH impact, title, stable identifier, and recognized schedule state
and date confidence. An explicit empty event list is accepted only if the
other freshness/coverage conditions are satisfied. Unknown, tentative or
disputed event scheduling, duplicate records, missing metadata, stale/partial
responses, network failure and unexpected API output cause `NEWS_UNKNOWN`.

The provider says its calendar is cached for 15 minutes and assembled from
public sources, without a completeness guarantee. A correct API response
does **not** prove that all macroeconomic news has been captured, and future
provider changes may make this adapter block new entries. No silent fallback
to another provider is permitted after a selected source fails.

## CI pilot

`.github/workflows/showmytrades-live-news-pilot.yml` tests historical FMP
compatibility and the new adapter entirely offline, verifies broker writes
remain disabled, then performs **one** public real-source GET with no broker
calls or order execution. Failing the live probe must block promotion.

This branch selects ShowMyTrades as the default runtime news source. It does
not enable broker order writes and does not fetch any historic news by itself.
Promotion to `main` requires successful CI and review.
