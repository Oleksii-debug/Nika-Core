# SQLite recovery ownership — ONE-SHOT-47

## Exact lineage

This batch started from live `main`
`3fbfabfc93d59183f174ff44098db886cff93bd8` and first converged the exact DEV28
#220 WAL/audit-continuity blobs without importing DEV29 #218 release-layer recovery.

The branch was then synchronized non-force as `main` advanced. The latest compatibility
sync parent for this evidence batch is
`23c7c1ce97b263b4aafa61bdcbace207b4476a3d`. The main movement after
`af43e41dca1066f95debafef360d61b2bf38b2ec` had no file delta; it only repaired
accidental main ancestry. No recovery bytes were taken from an unmerged sibling branch.

The canonical recovery authority remains
`nika_core.reliability.backup.SQLiteRecoveryManager`.

## Reuse / adapt / custom decision

**REUSE**

- SQLite Online Backup API for live, safety and restore copies;
- SQLite native locking (`PRAGMA locking_mode=EXCLUSIVE` plus `BEGIN EXCLUSIVE`);
- existing restore preview, safety backup, quarantine, marker, rollback and audit logic;
- stdlib OS advisory file locking only.

**ADAPT**

- DEV28 main+WAL preview commitment is retained and versioned as v2;
- missing WAL and a zero-byte WAL are the same logical durable state because obtaining
  SQLite exclusive ownership may itself create an empty WAL file;
- a non-empty WAL remains byte-for-byte part of restore authority;
- healthy restore uses one exclusive live SQLite connection continuously across final
  preview revalidation, safety backup, replacement, validation and rollback;
- canonical backup artifacts remain single-file manifest-bound SQLite snapshots and are
  revalidated under native SQLite ownership immediately before restore staging.

**CUSTOM (thin)**

- one persistent sibling recovery-lock file serializes recovery managers across
  processes, including missing/corrupt-target cases;
- a no-clobber hard-link publication primitive prevents a missing/quarantined target
  from being silently replaced if another creator wins the path race;
- quarantine rollback refuses to overwrite a target whose bytes are neither the exact
  pre-restore database nor the exact staged restore image.

No dependency, database migration, permission, R-level authority, credential, workflow
or release adapter is added.

## Healthy live database quiescence

For a healthy live database, destructive restore now requires two independent ownership
conditions:

1. the recovery file lease is held for the full operation;
2. SQLite itself grants an exclusive locking-mode connection.

The second condition is the cross-process SQLite-writer boundary. Existing ordinary
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
not unlinked on release; deleting a file-backed lock can split concurrent owners across
old and new inodes. POSIX uses non-blocking `flock`; Windows uses a one-byte non-blocking
`msvcrt.locking` lock. Process termination releases the OS lock.

The lock path must be a direct regular file. Symlink/reparse-point and non-regular lock
paths fail closed. The lock contains no database content or secret material.

This lease serializes `create_backup`, `prepare_restore`, `restore`, and
`recover_interrupted_restore` against another canonical recovery manager.

## Restore preview and WAL authority

The restore confirmation fingerprint binds both the raw main-file SHA used by existing
quarantine evidence and a versioned logical durable commitment over main+WAL bytes.
A committed WAL-only transaction therefore invalidates an older confirmation even when
the main-file SHA did not change.

A missing WAL and a zero-byte WAL are normalized to the same empty durable state because
SQLite may create an empty WAL while exclusive ownership is established. A non-empty WAL
is never normalized away. SHM is excluded from the content commitment because it is
transient coordination state, but unsafe SHM filesystem identity still fails closed.

Restore also rejects an indirect/non-regular target, indirect/non-regular WAL or SHM, and
orphan sidecars when the main database is absent.

## Backup source authority

A canonical backup manifest binds one SQLite database file by exact filename, byte size,
SHA-256 and schema version. WAL/SHM state is not part of that manifest, so a backup with
sidecars cannot be treated as the same authorized artifact.

`verify_backup()` therefore requires a direct database and manifest and rejects any
backup-side WAL/SHM before accepting the artifact. It rechecks sidecar coherence, size and
SHA after database validation so a mutation during verification does not silently pass.

Restore staging then closes the later source TOCTOU window:

1. require the manifest-bound backup to be sidecar-free;
2. obtain SQLite native exclusive ownership of the backup source;
3. revalidate direct file identity, exact size, exact SHA-256 and exact schema;
4. allow only a transient zero-byte WAL created by SQLite ownership; reject any non-empty
   WAL as durable state not bound by the manifest;
5. use SQLite Online Backup from that held source connection into the staged database;
6. revalidate the same source authority before releasing the lock.

Thus a committed WAL mutation of the backup after restore preview cannot be consumed as a
manifest-authorized restore source.

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

Rollback is also fail-closed against path races. If a target exists during quarantine
rollback, it may be treated as recoverable only when its SHA equals either the exact
pre-restore SHA or the exact staged-image SHA. Any third value is an unknown competing
target: it is preserved in place, the quarantine is preserved, and the durable restore
marker remains for explicit recovery rather than deleting another process's bytes.

## Audit continuity

Public backup still records `reliability.backup_created`. The restore-internal safety
backup deliberately uses the same canonical copy/verification implementation with audit
recording disabled, so it does not append a live event that would disappear when the live
database is replaced.

The final `reliability.restore_completed` event is appended to the fully staged database
before publication and records the exact source backup plus the generated safety-backup
filename. The surviving restored database therefore retains final restore evidence.

## Physical process-loss evidence

Exception-based fault injection remains useful for ordinary rollback paths, but it is not
accepted as the only crash proof because Python still executes `finally` blocks and closes
SQLite connections while unwinding an exception.

`tests/test_backup_restore_recovery_process_loss.py` therefore uses spawned processes and
`os._exit()` at exact recovery boundaries. The parent process proves durable filesystem
and SQLite state after the child exits without Python cleanup:

- a separate process holding an active WAL read transaction prevents native exclusive
  restore ownership;
- process loss after the safety backup but before staged audit/live copy preserves the
  live logical database and leaves a verifiable safety artifact;
- process loss immediately after SQLite Online Backup commits the staged database into the
  live connection leaves the restored logical state and final `restore_completed` audit
  recoverable on a fresh connection;
- process loss after a corrupt target has been quarantined but before staged publication
  leaves the marker, stage and quarantine sufficient for `recover_interrupted_restore()`
  to complete exactly once.

These are process-loss tests, not a claim of simulated power-loss or arbitrary hostile
filesystem containment.

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
- committed WAL mutation of a manifest-bound backup after preview;
- same-process and cross-process live WAL client exclusion;
- cross-process recovery-owner conflict;
- real `os._exit` process loss after safety backup but before live copy;
- real `os._exit` process loss immediately after committed healthy live copy with durable
  final audit;
- real `os._exit` process loss after quarantine and before staged publication, followed by
  interrupted-restore completion;
- crash after corrupt-stage publication followed by interrupted recovery;
- no-clobber publication race;
- unknown competing target preservation during quarantine rollback;
- unsafe/orphan/indirect sidecar rejection;
- existing safety-backup audit continuity, quarantine, rollback and interrupted-recovery
  suites.

Exact-head Core CI on Ubuntu+Windows and complete M12 remain required. Owner tests do not
self-clear AUD02/AUD03. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`;
`PRODUCTION_RELEASE_READY=false`.
