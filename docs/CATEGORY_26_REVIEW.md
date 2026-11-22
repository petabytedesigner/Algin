# Kategoria 26 — Broker reads, recovery, concurrency

Status: **implemented and verified with synthetic/offline tests**. No OANDA
Practice/Live network write has been released.

## Root cause and isolated changes

- `PracticeTransport.read()` previously made one GET attempt: a transient timeout,
  HTTP 429 or HTTP 5xx immediately failed account reconciliation. Its default
  HTTP adapter still has **zero automatic retries**, especially for writes.
- `utils.common.safe_request()` handled only connection exceptions, not
  retryable HTTP GET responses. Both read paths now share a small backoff
  policy (`utils/read_retry.py`), without changing callers or public reply types.
- A GET is attempted at most three times with 0.25s then 0.5s exponential
  backoff. HTTP 408/429/500/502/503/504 and network exceptions may retry.
  HTTP 4xx other than 408/429 do not retry. `Retry-After` on 429/503 is
  respected when <= 2s; larger delays **stop the attempt** and hold the
  read-only circuit open, up to one hour. No redirects are followed.
- After exhausted retries, invalid HTTP 200 JSON, or broker-mandated waiting,
  the read circuit blocks requests for at least 10 seconds. After cooldown
  only GET probes are permitted. **A successful HTTP status is insufficient
  to reset the circuit**: `AccountReconciler.run()` resets it only after the
  complete account snapshot, transactions, journal, state, and dependent
  protection checks pass, with necessary state persisted. Blocked snapshots,
  exceptions, or ambiguous intents do not reset it.

## Protections already present and retained

- Broker POST/PUT/PATCH/DELETE: no new retries. `PracticeTransport` still
  uses `HTTPAdapter(max_retries=0)` and `_PRACTICE_WRITES_RELEASED=False`.
  The order engine persists `IN_FLIGHT` before a possible mutation and treats
  unknown results as `UNKNOWN`, requiring read-only broker reconciliation.
- Durable intent journal, transaction cursor, checksummed state, and
  idempotent replay were already covered in Categories 3/4/7/23. No state
  schema, broker-order payload, or ledger contract changed.
- `ProcessLease` already rejects a second process and does not steal an
  apparently stale lease after crash. Its behavior is unchanged.
- A transient retry exhaustion is still **fail-closed**. A running service
  may exit with a blocked heartbeat; an external supervisor and verified
  restart remain operational requirements. No recovery code sends orders.

## Synthetic verification

`python -m unittest discover -s tests -q`

New Category 26 fault injection checks GET timeout/503 recovery, 429
`Retry-After`, circuit cooldown/validated reset, malformed JSON and 401,
single-attempt POST despite timeout, legacy GET resilience, malformed
account snapshot fail-closed, and process lease exclusion. Existing recovery
and concurrency tests cover crashes before durable state commit and restart
(`test_phase_e_category23.py`, `test_phase_b_category07.py`,
`test_phase_b_category08.py`, `test_category4.py`).

## Residual limits

- The read circuit is **process-local**; after a process restart, the service
  must reconcile before doing any management. This is not a distributed
  rate-limit coordination mechanism across machines.
- Date-form HTTP `Retry-After` values are not parsed; the request fails
  closed and the circuit cools down rather than attempting an early retry.
- Broker writes remain disabled. Full Practice integration, live service
  supervision, and profitability validation are not established by this work.
