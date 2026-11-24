# Kategoria 28 — Long-term audit evidence snapshots and incident lookup

Status: **copy-only archival implemented and tested offline**. This is NOT
automatic log rotation and NOT a release to live OANDA order execution.

## Reason for the boundary

`EventLog._existing()` enforces unique/idempotent `event_id` values **within
its active CSV**. `AuditProjector` may re-project the same durable broker
journal/lifecycle events on restart. Simply moving `events.csv` into an archive
would make those IDs invisible and permit historical duplicates. The strategy
decision trace also uses a checksum chain starting at its current first row.
Rotating either file without a verified cross-segment index and continuity
protocol would misrepresent audit history.

Category 28 therefore implements **verified snapshots without moving or
truncating source evidence**. Never treat a snapshot as permission to delete
active journal, account state, transaction cursor or logs.

## Implementation

- `operations/audit_archive.py` provides an explicit local operator command
  that snapshots the two known allowlisted sources: `events.csv` and
  `decision_trace.jsonl`. No broker/network dependency.
- Acquire the existing EventLog and DecisionTrace writer-lock paths plus an
  archive lock. Refuse occupied or replaced locks and symlink paths. Validate
  the full sources through their respective existing validators before copy.
- Store content-addressed compressed evidence (`*.gz`, mode `0600`) and a
  `fsync`-durable, checksummed manifest with source SHA-256, raw size, record count and
  capture timestamp. Manifests are linked by previous snapshot identity.
  Unchanged sources reuse already verified blobs; identical snapshots are
  idempotent. Publication never rewrites an existing archive blob or manifest.
- `verify` checks every published manifest, its predecessor chain and each
  decompressed source digest, failing closed on corruption or missing middle
  snapshots. `find` returns archived events/decisions for a given `cycle_id`
  or `signal_id`, deduplicating identical records across successive snapshots.
  It never calls risk, execution or broker APIs.
- Sensitive runtime files are excluded from Git (`.local/`,
  `csv/decision_trace.jsonl`). No API credentials, account snapshots, strategy
  payloads or raw broker replies are added to the archive manifest.

## Operator commands (run while the Algin service is stopped)

```bash
python -m operations.audit_archive snapshot --source-dir csv --archive-dir .local/audit-archives
python -m operations.audit_archive verify --archive-dir .local/audit-archives
python -m operations.audit_archive find --archive-dir .local/audit-archives --cycle-id YOUR_CYCLE_ID
python -m operations.audit_archive find --archive-dir .local/audit-archives --signal-id YOUR_SIGNAL_ID
```

Archive directory: `.local/audit-archives/` by default, excluded from Git.
Files are not encrypted. Operators must keep backups on a separate, securely
controlled device/storage, with encryption and retention suited to their use.
Never commit archives, runtime logs, credentials, or broker data to GitHub.

## Verification

`python -m unittest discover -s tests -q`.

Dedicated offline tests cover simultaneous CSV/JSONL capture, unmodified
source bytes, idempotent snapshots, unchanged blob reuse, forensic lookup,
tampered and missing manifests, corrupted blob, stale writer lock, symlinks,
and torn active evidence. No OANDA writes or network calls occur in tests.

## Important residual risks / deferred work

1. **Active file size limits remain 64 MiB.** Snapshots are copies, not
   rotation. When an active audit file fills, existing append logic will block
   rather than destroy evidence. A future separately verified cross-segment
   event-ID index, decision hash continuity and crash-safe cutover are needed
   before implementing retention/rotation. Do not bypass this safeguard.
2. A missing *latest* manifest or a total erasure of archive storage cannot
   be proven from the surviving local folder alone. External signed/immutable
   backups are needed for adversarial evidence custody. SHA-256 here detects
   accidental and local corruption, not a powerful attacker who can rewrite
   all manifests and hashes.
3. Snapshots are manual/offline; no scheduled retention, automatic deletion,
   remote replication, verification of recovered broker state, or automatic
   state restoration is implemented. A missing snapshot is not a broker state
   error; the active authoritative files remain the operational source.
4. `events.csv` and strategy decision trace are captured. Account-scoped
   `IntentJournal`, lifecycle state and forward-run journals are **not**
   copied by this feature: coordinated backup/restore of those operational
   state files requires its own broker-reconciliation-aware protocol.
