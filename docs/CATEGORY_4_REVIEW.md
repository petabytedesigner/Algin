# Category 4 — State, recovery and full trade-lifecycle management

Branch: development/category-4-state-recovery-management-2026-11-15
Base main: 5e72a1929351ca000ef080f3f2a0a9702e83347b

Scope: **P31 P32 P33 P34 P35 P36 P37 P38 P39 P40**.

## Scope and acceptance

| P | Implementation | Safety boundary |
|---|---|---|
| P31 | Read-only full OANDA Account Details, GET transactions/sinceid and exact transaction cursor checks; detect manual, pending, unaccounted positions | No absent-position inference; mismatch blocks |
| P32 | Atomic JSON state + SHA256 integrity, fsync of file and directory, expected cursor before update | Disk failure preserves prior state |
| P33 | TradeRecord includes broker trade ID, intent ID, signed original/current units, grade, original entry, original SL and risk, current SL/TP and BE/partial statuses | Missing provenance from journal rejects adoption |
| P34 | State version, account/environment and schema validation, checksum rejection, no legacy silent migration | Corrupt/wrong-account state never overwritten |
| P35 | Account-scoped exclusive lease for management process and durable intent uniqueness | Stale lease from crash requires manual reconciliation |
| P36 | ManagementLoop independent of new-entry session and new CLI --manage-once, main loop reconciles before scan | No trading in this category |
| P37 | Signed R uses original entry-to-SL distance; LONG bid, SHORT ask | Negative/adverse movement never becomes positive R |
| P38 | BE proposed only after positive threshold and no worse stop, one durable intent; STOP_LOSS_ORDER + broker snapshot required to confirm | No retry, no broker amendment yet |
| P39 | One partial intent per phase, OANDA positive close-unit magnitude for both LONG/SHORT, step precision and remainder protection; confirmation from ORDER_FILL tradeReduced | No close request sent; unknown result blocks |
| P40 | ORDER_FILL tradesClosed confirms final closure, net realizedPL (including partial), one-time outcome ledger and cooldown after loss | No count for absent trade without transaction |

## OANDA v20 contracts and precedents

- https://developer.oanda.com/rest-live-v20/best-practices/
- https://developer.oanda.com/rest-live-v20/account-ep/
- https://developer.oanda.com/rest-live-v20/account-df/
- https://developer.oanda.com/rest-live-v20/transaction-ep/
- https://developer.oanda.com/rest-live-v20/trade-ep/

Use a complete account snapshot + lastTransactionID as the basis, then
GET /accounts/{accountID}/transactions/sinceid?id=... for ordered transactions.
Account Details is non-atomic with separate follow-up GETs; if cursor or
transaction coverage cannot be trusted, state does not advance and management
is blocked. A missing trade from the open inventory is NEVER sufficient to
mark it closed.

Historical LogTX restart recovery logs show clean account, existing trades,
pending orders and recovery blocking, with read-only checks. Those logs are
precedent scenarios; they are **not** proof that this version executes live.

## Operational restrictions

- Real OANDA Practice POST/PUT/PATCH remains permanently disabled in this
  category at the lowest transport boundary, regardless of env/config flags.
- --manage-once performs GET requests and may write local durable state but
  never mutates broker account state; --check makes no network requests.
- main's perpetual loop now reconciles before checking entry-session hours.
  The legacy --once scan is unchanged and is **observation-only**, not recovery.
- BE/partial are previewed and may be staged as local durable intents in an
  isolated management API. Sending broker modifications is deferred to a
  separately authorized/verified operational release. A staged action must not
  be automatically repeated after timeout/restart.
- Initial reconstruction requires the original Category-3 order intent,
  a confirmed fill, grade, filled units, initial SL/TP and exact broker trade
  ownership. Older partial history without a trustworthy local record is
  rejected, never silently reverse-engineered from the current SL.
- No automatic deletion of a stale process lease, no automatic recovery from
  corrupted state, no auto-close and no retry of unknown broker operations.
- The result/cooldown ledger is a durable *per-trade* ledger, not a substitute
  for complete independently verified daily NAV and all-account P/L history.
- Full OANDA Practice integration (with real positions and transaction history)
  remains untested. Operational release is not authorized by passing CI.

## Merge gate

1. Verify main SHA and PR head SHA are the reviewed values.
2. Confirm all offline tests/compileall/check pass, including Categories 1–3.
3. Review code/data migrations and negative fixture coverage.
4. Obtain explicit approval before integrating the entire category into main.
5. Preserve observe-only release gate after merge.
