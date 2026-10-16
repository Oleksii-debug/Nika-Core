# Backup / Restore WAL Authority Hardening

## Scope

This MANUAL-DEV28 batch hardens the existing `SQLiteRecoveryManager` restore-preview
contract without introducing another persistence or backup framework.

Starting live main for the batch:
`e40691a6e2ff9c31fd413f63d004612e048d95ed`.

The changed production surface is limited to
`src/nika_core/reliability/backup.py`. M10 authorization/approval source owned by
PRs #61/#62, shared SQLite schema, Product Factory source, release workflows,
credentials, UI/UIA and coding-worker containment are not modified.

## Defect

A `RestorePlan` previously bound the approved live state to SHA-256 of only the
main SQLite database file.

That is insufficient in WAL mode. A committed transaction may exist in
`<database>-wal` while the main database bytes remain unchanged. The logical live
state can therefore change after `prepare_restore()` while the old confirmation
fingerprint still matches. A destructive restore could then consume authority that
was granted for a stale preview.

A deterministic oracle reproduces exactly that representation:

1. enable WAL and disable automatic checkpointing;
2. checkpoint/truncate to establish a stable main database file;
3. prepare the restore preview;
4. commit a task update into the WAL;
5. prove the main-file SHA-256 is still unchanged;
6. prove the task value is the newly committed value;
7. require `restore()` to reject the old confirmation fingerprint with
   `RestorePlanStaleError` and preserve the new live value.

## Repair

The public `RestorePlan.current_sha256` contract is preserved. It remains the raw
main-database SHA-256 used by corrupt-database quarantine and rollback evidence.

The internal confirmation fingerprint now additionally binds a versioned
`current_state_sha256` commitment. That commitment streams, with explicit framing:

- the main database bytes;
- existence and byte length of the main database;
- the WAL bytes when a WAL file exists;
- existence and byte length of the WAL representation.

The transient `-shm` file is deliberately excluded from the digest because it is
SQLite coordination state rather than durable database content. A checkpoint that
moves an unchanged logical state between WAL and the main file can invalidate a
preview; that conservative false-stale result is acceptable for a destructive
operation because the caller can create a new preview.

No temporary full database copy is created merely to compute this state commitment,
so the repair does not introduce a new crash-leftover copy containing private data
and does not require memory proportional to database size.

## SQLite family fail-closed rules

Before preview and again before restore, Nika now rejects:

- a restore target path that exists but is not a regular file;
- WAL/SHM symbolic links;
- WAL/SHM paths that exist but are not regular files;
- an absent main database accompanied by orphan WAL/SHM sidecars.

The last case is not treated as a clean `current_exists=false` state because SQLite
sidecars can represent recovery-relevant state and must not be silently overlaid by
a new database.

## REUSE -> ADAPT -> CUSTOM(thin)

- **REUSE:** existing SQLite WAL semantics, current `SQLiteRecoveryManager`, Python
  streaming `hashlib.sha256`, `pathlib`, and existing restore preview/confirmation
  contract.
- **ADAPT:** extend the current optimistic restore fingerprint from main-file bytes
  to the durable SQLite main+WAL representation.
- **CUSTOM (thin):** only versioned Nika framing and fail-closed SQLite-family
  validation.

No dependency, migration, permission, approval level, secret store or external
service is added.

## Concurrency boundary

This batch must not be described as a complete cross-process restore lock.

The new state fingerprint detects committed WAL/main-byte changes that are present
when `restore()` revalidates the preview. The current repository does not yet expose
a universal quiescence/lease primitive that every SQLite writer, including direct
SQLite users, is required to hold through destructive replacement. A separate
compatibility decision would be required before changing the shared `SQLiteStore`
connection contract or claiming atomic exclusion of every external writer during
the remaining replacement interval.

This limitation does not justify ignoring committed WAL state. The present repair
closes a deterministic stale-authority class while preserving the stronger future
quiescence design boundary.

## Focused evidence

`tests/test_backup_restore_wal_stale_preview.py` covers:

- committed WAL-only state change after preview while raw main SHA remains equal;
- rejection of the stale confirmation before destructive replacement;
- preservation of the post-preview live task value;
- orphan WAL sidecar rejection when the main database is absent.

Existing `tests/test_backup_restore_guardrails.py` and
`tests/test_backup_restore_recovery.py` remain broad regression authority for
backup verification, safety backups, migration, corruption quarantine, rollback
and interrupted-restore recovery.

Repository acceptance still requires exact-head Linux and Windows Core CI plus the
applicable M12 gate. Automated evidence does not set `HUMAN_TESTED` or
`NVDA_VERIFIED`.
