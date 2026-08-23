# SQLite recovery ownership — ONE-SHOT-47

## Exact starting point

This batch started from live `main`
`3fbfabfc93d59183f174ff44098db886cff93bd8` and first converged the exact DEV28
#220 WAL/audit-continuity blobs without importing DEV29 #218 release-layer recovery.

The canonical recovery authority remains
`nika_core.reliability.backup.SQLiteRecoveryManager`.

## Reuse / adapt / custom decision

**REUSE**

- SQLite Online Backup API for live/safety/restore copies;
- SQLite native locking (`PRAGMA locking_mode=EXCLUSIVE` plus `BEGIN EXCLUSIVE`);
- existing restore preview, safety backup, quarantine, marker, rollback and audit logic;
- stdlib OS advisory file locking only.

**ADAPT**

- DEV28 main+WAL preview commitment is retained and versioned as v2;
- missing WAL and a zero-byte WAL are the same logical durable state because obtaining
  SQLite exclusive ownership may itself create an empty WAL file;
- a non-empty WAL remains byte-for-byte part of restore authority;
- healthy restore uses one exclusive live SQLite connection continuously across final
  preview revalidation, safety backup, replacement, validation and rollback.

**CUSTOM (thin)**

- one persistent sibling recovery-lock file serializes recovery managers across
  processes, including missing/corrupt-target cases;
- a no-clobber hard-link publication primitive prevents a missing/quarantined target
  from being silently replaced if another creator wins the path race.

No dependency, database migration, permission, R-level authority, credential, workflow
or release adapter is added.

## Healthy live database quiescence

For a healthy live database, destructive restore now requires two independent ownership
conditions:

1. the recovery file lease is held for the full operation;
2. SQLite itself grants an exclusive locking-mode connection.

The second condition is the important cross-process writer boundary. Existing ordinary
SQLite clients do not need a new Nika API: they already participate in SQLite file/WAL
locking. An existing WAL reader/writer that prevents exclusive ownership therefore makes
restore fail closed before the safety backup or live replacement.

The exclusive connection stays open while:

`revalidate preview -> safety backup -> append restore_completed to stage -> online backup stage into live -> validate live`

If an ordinary exception occurs after the live copy, rollback from the safety backup is
performed through that same exclusive destination connection before it is released.
An abrupt process loss during SQLite Online Backup relies on SQLite transaction atomicity;
a loss after the copy has committed leaves the complete restored database, including the
pre-publication `reliability.restore_completed` event.

## Recovery-manager ownership

`.<database>.nika-recovery.lock` is a persistent sibling lock file. It is intentionally
not unlinked on release; deleting a lock file can split concurrent owners across old and
new inodes. POSIX uses non-blocking `flock`; Windows uses a one-byte non-blocking
`msvcrt.locking` lock. Process termination releases the OS lock.

The lock path must be a direct regular file. Symlink/reparse-point and non-regular lock
paths fail closed. The lock contains no database content or secret material.

This lease serializes `create_backup`, `prepare_restore`, `restore`, and
`recover_interrupted_restore` against another canonical recovery manager.

## Publication, quarantine and interrupted recovery

When no live target exists, and when the corrupt-target path has already moved the old
bytes into quarantine, the staged database is published with a same-directory hard link.
That operation is atomic and no-clobber: if another creator has produced the target path,
recovery fails instead of overwriting it. The stage is already migrated, integrity-checked
and contains final restore audit evidence before publication.

The existing durable marker remains authoritative for corrupt-target replacement. A crash
after quarantine but before publication can resume; a crash after stage publication but
before manifest/marker cleanup is recognized by the exact staged SHA and completed without
repeating replacement.

## Sidecar and preview authority

Restore fails closed for:

- an indirect or non-regular target path;
- indirect, non-regular WAL/SHM sidecars;
- WAL/SHM sidecars with a missing main database;
- a committed non-empty WAL change after preview.

SHM remains excluded from the logical content digest because it is transient coordination
state, but unsafe SHM filesystem identities still fail closed.

## Explicit residual boundary

This work does **not** claim an authenticated filesystem transaction or a sandbox against
an unrestricted process that rewrites/renames database files outside SQLite semantics.

For a truly corrupt/non-SQLite current target, SQLite cannot grant a meaningful native
exclusive database lock. The explicit destructive override path therefore retains the
recovery file lease plus durable quarantine/rollback marker, but it is not described as
universal SQLite-client quiescence. Likewise POSIX advisory file locks cannot constrain a
hostile process that deliberately ignores them.

The closed cross-process gap is the canonical healthy SQLite-client case: ordinary SQLite
connections, including direct `sqlite3` clients, must permit native exclusive ownership
before the recovery manager changes the live logical database.

## Evidence requirements

Focused deterministic tests cover:

- WAL-only stale confirmation and zero-WAL normalization;
- live WAL client exclusion;
- cross-process recovery-owner conflict;
- crash after safety backup but before live copy;
- crash after committed healthy live copy with durable final audit;
- crash after corrupt-stage publication followed by interrupted recovery;
- no-clobber publication race;
- unsafe sidecar rejection;
- existing safety-backup audit continuity, quarantine, rollback and interrupted-recovery
  suites.

Exact-head Core CI and M12 remain required. Owner tests do not self-clear AUD02/AUD03.
`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
