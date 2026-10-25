# Release database recovery adapter

Nika Core release/update rollback must reuse the canonical M10 database recovery service.
`nika_core.packaging.recovery.ReleaseDatabaseRecovery` is a release-identity adapter over
`nika_core.reliability.backup.SQLiteRecoveryManager`; it is not a second SQLite recovery engine.

## Release-specific contract

A committed release snapshot contains exactly:

- `database.sqlite3`;
- `database.sqlite3.manifest.json`, created and verified by the canonical recovery manager;
- `release-database-snapshot.json`, created by the packaging adapter.

The release metadata binds the canonical `BackupArtifact` to one exact lowercase 40-character
source release SHA. It records the canonical backup SHA-256, byte size, schema version and
creation timestamp. Existing snapshot paths are idempotent only when the verified release
identity is unchanged. Partial, extra, malformed, tampered or differently-bound state fails
closed.

The adapter never treats a caller-supplied SHA as a signature. Trusted source/current release
identity must come from the release/update control plane.

## Restore flow

The adapter preserves this authority order:

`verify release snapshot -> canonical prepare_restore -> bind source/current release identity ->
release confirmation -> reverify release snapshot -> canonical restore`

When a live database exists, an exact current release SHA is mandatory. When no live database
exists, a current release SHA is rejected. The release confirmation fingerprint binds:

- source release SHA;
- canonical snapshot database SHA-256;
- current release SHA or explicit absence;
- canonical `RestorePlan.confirmation_fingerprint`.

Destructive replacement, safety backup, migration, quarantine, rollback and interrupted-restore
recovery remain owned by `SQLiteRecoveryManager`.

## Canonical recovery dependency

Current integration base for this DEV29 successor is
`23c7c1ce97b263b4aafa61bdcbace207b4476a3d`.

The integrated base still exposes the public M10 API used by this adapter, but the stronger
current canonical recovery convergence is owned separately by ONE-SHOT-47 PR #311 (or its live
successor). That lane carries the WAL/restore-authority work descended from DEV28 #220, including
WAL representation binding, recovery-owner serialization, native SQLite exclusion and
no-clobber publication.

DEV29 does not copy those mechanisms into packaging. The adapter must remain on dependency hold
until the canonical recovery successor is integrated. After that integration, this adapter must
be refreshed against the new `main` and requalified on one exact head before release acceptance.

## REUSE -> ADAPT -> CUSTOM(thin)

**REUSE:** `SQLiteRecoveryManager`, `BackupArtifact`, `RestorePlan`, `RestoreResult`, canonical
backup verification and canonical restore/restart behavior.

**ADAPT:** bind canonical backup evidence and restore previews to exact release identities.

**CUSTOM(thin):** strict release metadata, exact release-SHA validation, atomic metadata/snapshot
publication and one release-level confirmation fingerprint.

No second database, SQLite copy algorithm, restore state machine, migration stream, lock service,
workflow authority, permission, credential path or external dependency is introduced.

## Acceptance boundary

Focused tests cover exact release binding, idempotent replay, metadata strictness, canonical
tamper detection, source/current release confirmation, Unicode/space paths, missing-live
semantics, canonical stale-preview propagation and the architecture oracle that packaging does
not implement SQLite recovery itself.

Repository acceptance requires dependency consistency, Ruff, compile, Core on Ubuntu and Windows,
M11, complete M12 and fresh current-main compatibility on one exact candidate. The final release
still requires the normal manifest/checksum/notices/security and packaged accessibility gates.

Automated evidence never sets `HUMAN_TESTED` or `NVDA_VERIFIED`.
