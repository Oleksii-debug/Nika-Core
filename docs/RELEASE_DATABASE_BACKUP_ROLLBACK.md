# Release database recovery adapter

Nika Core release/update rollback must use the canonical M10 database recovery service.
The packaging layer does not own a second SQLite backup or restore engine.

## Canonical authority

The authoritative database recovery implementation is
`nika_core.reliability.backup.SQLiteRecoveryManager`.

It already owns:

- SQLite online backup through `sqlite3.Connection.backup()`;
- backup manifest creation and verification;
- SQLite integrity, foreign-key and migration-history validation;
- restore preview and confirmation fingerprinting;
- migration of a staged older backup before live replacement;
- automatic safety backup of a healthy current database;
- corrupt-database quarantine;
- staged replacement and rollback;
- durable interrupted-restore markers and `recover_interrupted_restore()`;
- recovery audit events.

`nika_core.packaging.recovery.ReleaseDatabaseRecovery` is therefore an adapter over that
public service. It must not copy those mechanisms into packaging code.

## Release-specific responsibility

The packaging adapter adds only evidence that M10 does not own:

1. bind one canonical backup artifact to an exact lowercase 40-character source release SHA;
2. publish that binding with the canonical backup database and canonical backup manifest as one
   release snapshot directory;
3. make an existing snapshot path idempotent but never silently rebind it to another release;
4. bind the canonical restore-preview fingerprint together with source and current release SHAs;
5. reverify release metadata immediately before delegating destructive restore;
6. delegate interrupted-restore recovery back to `SQLiteRecoveryManager`.

The release metadata is not a signature. The source/current release SHA values must come from
the trusted release/update control plane. Candidate state cannot turn a self-supplied SHA into
trusted release authority merely by writing this metadata.

## Snapshot format

A committed release database snapshot contains exactly:

- `database.sqlite3`;
- `database.sqlite3.manifest.json`, created and verified by `SQLiteRecoveryManager`;
- `release-database-snapshot.json`, created by the packaging adapter.

The release metadata records:

- metadata schema version;
- exact source release SHA;
- canonical database and manifest filenames;
- canonical backup SHA-256;
- canonical backup size;
- canonical schema version;
- canonical creation timestamp.

The adapter does not calculate a competing database hash or schema interpretation. Verification
calls `SQLiteRecoveryManager.verify_backup()` and compares its returned `BackupArtifact` with
the release metadata.

Snapshot publication uses a sibling staging directory and atomic directory rename. An existing
snapshot is verified and returned for an exact retry. Invalid, partial, unexpected or
different-release state fails closed and is not overwritten.

## Restore flow

The release adapter follows this sequence:

`verify release snapshot -> canonical prepare_restore -> bind release identities -> approval /
confirmation -> reverify release snapshot -> canonical restore`

When a live database exists, the caller must provide the exact current release SHA. When no live
database exists, a current release SHA is rejected. This prevents one confirmation object from
being ambiguous between "replace release X" and "install into an empty state".

The release-level confirmation fingerprint includes:

- source release SHA;
- snapshot database SHA-256;
- current release SHA or explicit absence;
- the canonical M10 restore confirmation fingerprint.

`ReleaseDatabaseRecovery.restore()` does not replace the canonical confirmation. It verifies
the release-level binding and then passes the original canonical fingerprint to
`SQLiteRecoveryManager.restore()`.

The canonical result remains authoritative for:

- safety backup identity;
- restored schema version;
- corrupt-state quarantine;
- interrupted-restore markers;
- rollback behavior.

A future updater must keep its own durable install-operation journal that binds those canonical
recovery artifacts to the update operation. This adapter does not claim package-install,
process-restart or deployment authority.

## WAL compatibility decision

At current main `e40691a6e2ff9c31fd413f63d004612e048d95ed`, the canonical M10 restore preview
binds the main SQLite file. Independent DEV28 PR #220 owns a deterministic hardening that also
binds committed WAL representation and rejects unsafe orphan/indirected sidecars.

DEV29 does not edit or duplicate `src/nika_core/reliability/backup.py`. The packaging adapter
uses only the public `SQLiteRecoveryManager` API, so the DEV28 repair is inherited automatically
after integration.

Known-defect rule: this release adapter must not be treated as production-release-ready while
the canonical WAL stale-preview defect remains unresolved in the integrated base. If #220 or
its successor lands before this adapter, DEV29 must refresh from that new main and rerun exact
acceptance. If this adapter lands first, release readiness still waits for the canonical M10
repair.

## REUSE -> ADAPT -> CUSTOM(thin)

**REUSE**

- `SQLiteRecoveryManager`;
- `BackupArtifact`, `RestorePlan`, `RestoreResult`;
- canonical SQLite validation, backup, safety backup, migration, quarantine and crash recovery.

**ADAPT**

- canonical backup evidence into an exact-release snapshot directory;
- canonical restore preview into an exact source/current release confirmation.

**CUSTOM (thin)**

- release metadata schema;
- exact release-SHA validation;
- atomic metadata publication;
- release-level confirmation fingerprint.

No second database, migration stream, SQLite copy algorithm, restore state machine, OS recovery
lock, dependency, credential path, workflow authority or permission mechanism is introduced.

## Evidence boundary

Focused adapter tests must prove:

- canonical backup files are actually present and verified;
- exact release binding and idempotent replay;
- metadata corruption/unknown fields fail closed;
- canonical tamper detection remains authoritative;
- current-release identity is mandatory exactly when current database state exists;
- wrong release-level confirmation does not invoke restore;
- canonical stale-preview detection remains effective through the adapter;
- packaging source does not import or implement `sqlite3` recovery logic.

Exact repository acceptance remains Core CI on Ubuntu + Windows and the applicable M11/M12
packaged release gates on one exact head.

Automated package/recovery evidence never sets `HUMAN_TESTED` or `NVDA_VERIFIED`.
