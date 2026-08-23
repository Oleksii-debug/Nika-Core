# Release database backup and rollback contract

Nika Core release update/rollback must preserve durable user state before a candidate can
apply database migrations. This contract is deliberately thin and reuses SQLite's supported
online backup API rather than introducing a second database or migration framework.

## Scope and authority

`nika_core.packaging.recovery` provides three release-maintenance primitives:

1. `create_database_snapshot()` creates a consistent SQLite backup directory bound to the
   exact 40-character source release SHA.
2. `verify_database_snapshot()` verifies the manifest schema, release identity, file and
   logical-content SHA-256 evidence, migration-version evidence, and `PRAGMA integrity_check`.
3. `restore_database_snapshot()` restores only a verified snapshot. Replacing an existing
   database requires a second snapshot of the current state first.

These APIs do not download or select a release. They do not grant update/deployment authority,
change R0-R4 approval policy, or claim that a candidate ZIP is trusted. Package selection,
attestation, approval, and update orchestration remain separate release-control concerns.

## Snapshot format

A snapshot is one committed directory containing exactly:

- `database.sqlite3` — SQLite online-backup output;
- `snapshot-manifest.json` — exact source release SHA, file SHA-256, logical SQLite-content
  SHA-256, byte size, and known migration-version evidence.

Unexpected snapshot-directory entries fail closed. Verification opens already-committed backup
bytes as immutable SQLite input so a WAL-mode source cannot cause verification itself to create
`-wal` or `-shm` files inside the snapshot.

Creation occurs in a sibling staging directory. Both files are flushed before the staging
directory is atomically renamed to the requested snapshot directory. An already-existing valid
snapshot at the same path is treated as an idempotent replay; invalid or mismatched evidence
fails closed and is never overwritten.

The logical-content digest is computed from another SQLite backup of the database rather than
from the live database file bytes. This makes the comparison include committed WAL state and
lets restart recovery compare a current database with an earlier preservation snapshot even
when their physical SQLite file layout differs.

## Restore and restart semantics

Snapshot creation may run against a live SQLite database and captures committed WAL content.
Restore is intentionally stricter: the application must be quiescent. Existing `-wal`, `-shm`,
or `-journal` sidecars block restore so stale SQLite sidecars cannot be applied to replaced
bytes.

Each restore is serialized by a non-blocking operating-system file lock associated with the
destination database. A second concurrent recovery owner fails closed instead of racing the
preservation or replacement boundary. The operating system releases the lock on process exit;
the lock file itself may remain as harmless coordination metadata.

If the destination database already exists and differs from the rollback snapshot, callers
must supply both its exact current release SHA and a distinct preservation directory. Nika
creates/verifies that preservation snapshot before replacing any database bytes.

Restart behavior is fail-closed and replayable:

- crash before preservation commit: current database remains authoritative;
- crash after preservation but before replacement: replay accepts the preservation only if
  its logical-content digest still equals the current database;
- current database changed after preservation: replay blocks for reconciliation;
- crash after replacement: replay recognizes that the destination already matches the target
  snapshot and returns `already_restored=True` without overwriting preservation evidence.

The restore file is copied to a same-directory staging file, flushed, fully reverified, and then
installed with atomic `os.replace()`. The installed database is reverified before success is
reported.

## Integrity and threat model

SHA-256 evidence detects accidental damage and inconsistent/mismatched local recovery data. It
is not an authenticated signature against an attacker who can rewrite both the database and its
manifest. Trusted release artifact provenance remains the responsibility of the M12 attestation
path and release policy.

Snapshot source files, snapshot directories, manifests, and databases reject direct symlink or
Windows reparse-point indirection. Paths with spaces and Unicode are passed as filesystem/API
arguments and never interpolated into a shell command.

## Update integration boundary

The safe updater sequence is:

`trusted exact release -> approval/policy -> quiesce -> pre-update snapshot -> install candidate
-> migrate/start -> smoke/integrity -> commit update OR preserve failed state -> rollback`

This batch implements the backup/verify/restore foundation only. A later DEV29 updater must use
these primitives and add its own durable operation journal/reconciliation around package install
and process restart. It must not silently promote PR artifacts or stale ZIPs.

Automated backup/restore/package tests do not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
