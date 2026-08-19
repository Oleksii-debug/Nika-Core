# Backup, restore and corruption recovery

Updated: 2026-08-19.

Status: AUTO01 implementation candidate. This document describes engineering behavior only.
No milestone, HUMAN_TESTED or NVDA_VERIFIED claim follows from this source change.

## Failure family

Nika uses SQLite as the authoritative local product-state store. Backup/restore therefore has
to protect task state, runtime routing, idempotency records, memory, schedules, resource
budgets, audit events and other schema-owned data as one consistent database image.

The failure family addressed here is broader than "copy a file":

- a raw filesystem copy can miss committed WAL state or capture an inconsistent moment;
- a backup file can be truncated, tampered with, structurally damaged, contain foreign-key
  violations, or have a broken/missing migration history;
- a restore source can be valid but older than the current application schema;
- the live database can change after a user/operator previews a restore;
- restore can fail after the current database has already been changed;
- the current database can be so corrupt that SQLite cannot even open its header;
- the process can disappear between quarantining corrupt bytes and installing the staged
  known-good database.

The implementation fails closed for these cases instead of treating "file exists" as proof
that a backup is usable.

## REUSE / ADAPT / CUSTOM

**REUSE** Python's standard-library `sqlite3.Connection.backup()` and SQLite's Online Backup
API. Python documents that the backup operation works while the source database is being
accessed by other clients. SQLite documents that the destination is held in a write
transaction during the backup operation and an incomplete destination backup is rolled back.

Primary sources:

- Python sqlite3 backup:
  https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup
- SQLite Online Backup API overview:
  https://www.sqlite.org/backup.html
- SQLite backup API transaction/rollback semantics:
  https://www.sqlite.org/c3ref/backup_finish.html
- SQLite integrity and foreign-key checking:
  https://www.sqlite.org/pragma.html#pragma_integrity_check
  and https://www.sqlite.org/pragma.html#pragma_foreign_key_check

**REUSE** `PRAGMA integrity_check` for full database consistency checking and
`PRAGMA foreign_key_check` separately because SQLite explicitly documents that
`integrity_check` does not report foreign-key violations.

**REUSE** Nika's existing `SQLiteStore`, ordered `MIGRATIONS`, `SCHEMA_VERSION` and
`AuditLog`. No new dependency, database engine or competing migration framework is added.

**CUSTOM (thin)** Nika policy is limited to backup manifests, exact restore-preview binding,
pre-restore safety snapshots, quarantine/recovery markers and fail-closed recovery decisions.
These semantics belong to Nika because an upstream SQLite API cannot decide which Nika
schema versions, approvals or recovery outcomes are acceptable.

## Backup creation

`SQLiteRecoveryManager.create_backup()` follows this order:

1. refuse operation while an interrupted destructive restore marker exists;
2. create a temporary destination through SQLite's online backup API;
3. run full SQLite integrity checking;
4. run foreign-key checking;
5. require a non-empty, contiguous Nika migration history;
6. reject a database newer than this Nika build;
7. compute SHA-256 and exact byte size;
8. write a strict sidecar manifest containing format, filename, SHA-256, size, schema version
   and timezone-aware creation time;
9. flush temporary files and publish them with atomic same-directory replacement;
10. append best-effort audit evidence to the live database.

A backup database without its exact manifest is not accepted. An interrupted publication can
therefore leave an unusable orphan, but never an artifact that verification mistakes for a
valid backup.

## Verification

`verify_backup()` validates both bytes and semantics before restore is allowed:

- exact manifest field set and supported manifest format;
- manifest filename matches the selected database file;
- positive exact size;
- well-formed SHA-256 and constant-time digest comparison;
- `PRAGMA integrity_check` returns only `ok`;
- `PRAGMA foreign_key_check` returns no rows;
- migration versions are exactly `1..N` with no gaps;
- schema `N` is not newer than the running application;
- manifest schema version equals the database migration history;
- `created_at` is parseable and timezone-aware.

A manifest hash alone is intentionally insufficient proof.

## Restore preview and exact confirmation binding

Restore is a destructive state-changing operation, so `prepare_restore()` does not immediately
write the selected backup over live state.

It first verifies the backup and classifies the current database. A confirmation fingerprint
is derived from:

- selected backup SHA-256;
- exact resolved target path;
- whether the current database exists;
- exact SHA-256 of the current database after preview audit evidence is committed.

`restore()` requires that exact fingerprint. It verifies the backup again and recomputes the
live fingerprint. Any unrelated database change after preview produces
`RestorePlanStaleError` before live replacement begins.

This is a low-level safety primitive, not a UI approval claim. Product UI must still explain
the preview and obtain any policy-required explicit confirmation before passing the exact
fingerprint back.

## Healthy-current restore

For a readable supported current database:

1. copy the verified backup to a private staging database;
2. apply Nika's normal ordered migrations to the stage;
3. fully revalidate the migrated stage;
4. create a pre-restore safety backup of current state;
5. append `reliability.restore_completed` evidence into the staged database;
6. copy staged state into the live database through SQLite's backup API;
7. fully validate the resulting live database;
8. on a post-copy failure, copy the safety backup back and validate the rollback.

The safety backup remains on disk after success. It is not silently deleted because it is the
operator's direct rollback artifact for the state that existed immediately before restore.

## Unrecoverable-current restore

A completely malformed current file may fail before SQLite can open its header. SQLite's
transactional destination backup path cannot safely overwrite that case.

Nika therefore refuses it by default. `allow_replace_unrecoverable_current=True` is a separate
explicit recovery decision and uses a durable quarantine protocol:

1. verify the exact corrupt-current hash still matches the preview;
2. prepare and validate the known-good staged current-schema database;
3. write and flush a restore-in-progress marker containing only safe basenames and exact
   current/staged/backup hashes;
4. atomically move the current database into a uniquely named quarantine;
5. move any current `-wal` and `-shm` sidecars into matching quarantine names;
6. install the staged database;
7. validate its exact expected hash and SQLite/Nika schema integrity;
8. write a quarantine manifest that marks the old bytes as `trusted_database=false`;
9. remove the restore marker only after the new live database has validated.

Quarantine bytes are retained after successful recovery. They are diagnostic/rollback
evidence, not a database Nika should reopen automatically.

## Crash/restart recovery

A process loss may happen after the durable restore marker is written. On the next controlled
startup, `recover_interrupted_restore()` reads that marker and validates its field set,
basenames and SHA-256 values before acting.

It then reconciles observable filesystem state:

- staged bytes already installed and matching the marker -> validate/finalize success;
- old live bytes still present and stage intact -> continue the already-confirmed operation;
- live file absent, quarantine present and stage intact -> install the stage and finalize;
- stage unavailable but quarantine intact -> roll back exact old bytes;
- unknown live bytes with a valid quarantine -> roll back rather than guess;
- missing/inconsistent evidence with no safe path -> fail closed for operator diagnosis.

Rollback reproduces the exact pre-restore database SHA-256 and restores quarantined WAL/SHM
sidecars when they exist.

## Deterministic fault coverage

The AUTO01 test family covers:

- live WAL backup includes committed state while the writer connection remains open;
- byte tampering is rejected before restore;
- foreign-key corruption is rejected;
- migration-history gaps are rejected;
- wrong restore confirmation is rejected;
- live-state mutation after preview makes the restore plan stale;
- healthy restore preserves a verified safety backup and restores selected state;
- a valid version-1 backup is migrated to the current schema before live replacement;
- corrupt current bytes require the explicit destructive-recovery flag and are quarantined;
- simulated abrupt process loss between quarantine and stage installation is completed
  deterministically by a recreated recovery manager;
- injected post-copy validation failure rolls the healthy database back from its safety backup.

These are automated engineering proofs only.

## Operational limits and non-claims

The implementation does not claim protection from arbitrary hardware/controller failure after
the operating system reports a flush complete. File flushes are requested; directory fsync is
used where the platform exposes the POSIX directory-fd primitive. No stronger Windows storage
durability guarantee is claimed.

Restore is an offline maintenance primitive with respect to Nika database writers. The exact
preview hash catches changes before restore begins, and SQLite serializes the destination
backup transaction, but this module does not create an operating-system-wide lock that forces
unrelated processes to stop opening the database. Product wiring must stop Nika writers before
a destructive restore/recovery action. A future shared maintenance-mode contract may close
that remaining coordination window if runtime integration proves it necessary.

No OS sandbox is claimed. No secret, cookie, token, browser profile, credential or private log
material is introduced by this change.

`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
