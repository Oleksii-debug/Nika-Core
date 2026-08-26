# Windows local update and rollback

Status: ONE-SHOT-55 implementation candidate. This document records engineering behavior only; it does not grant release, human, or NVDA acceptance.

## Scope and architecture

Nika local Windows package update is deliberately a thin orchestration layer over existing release and recovery authorities.

**REUSE**
- canonical `nika_core.packaging.release.verify_distributable_evidence()` for the exact outer artifact/evidence binding;
- canonical `nika_core.packaging.release.verify_release_archive()` for final ZIP/member/manifest/source-SHA validation;
- canonical `SQLiteStore.initialize()` ordered migrations;
- canonical `SQLiteRecoveryManager` backup, verification, restore-preview confirmation, safety rollback, quarantine, and interrupted-restore recovery;
- canonical `AuditLog` `reliability.restore_completed` receipt as post-crash restore evidence.

**ADAPT**
- the exact release candidate is copied into a private operation directory before verification and use;
- canonical pre-update database backup is sequenced before migration/package replacement;
- canonical restore confirmation is persisted inside the updater operation journal so a restart cannot silently obtain a fresh destructive restore authorization;
- final M11/M12 Windows ZIPs are exercised through the same updater path before acceptance evidence is uploaded.

**CUSTOM (thin)**
- one update operation identity;
- a cross-process install update lease;
- an atomic/fsync-backed JSON operation journal;
- same-volume package-directory replacement and rollback sequencing;
- a host-provided startup/health boundary.

The updater does **not** implement another ZIP verifier, SQLite backup engine, migration framework, deployment fabric, credential authority, or release signer.

## Operation identity and durable journal

An operation ID binds the resolved installation directory, resolved database path, product, target version, and exact target source SHA. The operation owns a private copy of the candidate ZIP/evidence and a canonical pre-update backup.

Durable phases are:

`CANDIDATE_VERIFIED -> BACKUP_CREATED -> MIGRATION_STARTED -> MIGRATED -> REPLACEMENT_STARTED -> REPLACED -> HEALTH_CHECKING -> COMPLETED`

Rollback phases are:

`ROLLBACK_PACKAGE -> ROLLBACK_DATA_PREPARED -> ROLLBACK_DATA -> RESTARTING_OLD -> ROLLED_BACK`

An unrecoverable/ambiguous state becomes `BLOCKED`; the implementation does not guess or silently restart from a new authority source.

Repeated invocation of an already completed operation returns its durable terminal result only after revalidating that the installed package still matches the journal identity.

## Candidate verification

Before any backup/migration/package mutation:

1. the selected ZIP and its evidence file must exist;
2. current installed product identity must be valid and different from the target source SHA;
3. candidate ZIP/evidence are copied into the private operation directory;
4. outer distributable evidence verifies exact source SHA/path/size/hash;
5. the canonical final ZIP verifier checks the embedded release manifest and exact members;
6. the embedded product/version/source SHA must equal the host-authorized target;
7. the verified candidate is extracted to a same-volume private staging directory.

Candidate-controlled release SHA is correlation evidence, not trusted signing authority. Trusted-main provenance/attestation remains a separate release-control-plane responsibility.

## Backup and migration

The updater calls `SQLiteRecoveryManager.create_backup()` before running the candidate migration port. It never copies the live SQLite file itself.

The default migration port calls canonical `SQLiteStore.initialize()` and requires the resulting main schema to equal the supported current schema.

If migration raises, rollback begins immediately. The exact pre-update backup remains the rollback source.

## Package replacement and crash windows

Package replacement uses deterministic same-parent-directory identities:

- verified candidate stage;
- pre-update package rollback directory;
- failed-candidate evidence directory.

The updater writes `REPLACEMENT_STARTED` before the first directory move. Therefore process loss can be reconciled from observable package identities:

- old install still live -> move it to rollback then install stage;
- old already moved, stage present -> install stage;
- candidate already installed and rollback present -> validate identities and continue;
- unknown/changed install or missing rollback/stage evidence -> `BLOCKED`.

The package swap does not claim to be one atomic filesystem transaction. Durability comes from the pre-effect journal plus deterministic exact-identity reconciliation.

## Startup and health

After the candidate is installed, the updater enters `HEALTH_CHECKING` and calls a narrow host `StartupHealthPort`. Any provider-specific startup implementation is outside this state machine.

M11/M12 evidence uses a real packaged Windows health adapter which launches the replaced `NikaCore.exe --pf11-proof` against the same migrated database and requires a valid zero-exit JSON proof before the update may reach `COMPLETED`.

## Automatic rollback authorization boundary

Starting an update authorizes rollback **only to the updater's exact canonical pre-update backup** if migration/startup/health fails. It does not authorize arbitrary database replacement or a caller-selected unrelated restore source.

The pre-update backup is created and verified before migration. If the updater later finds the live database corrupt because the candidate/migration path failed, it may pass the canonical destructive-recovery flag only for restoring that exact pre-update backup within the same durable operation. `SQLiteRecoveryManager` still owns quarantine, exact restore confirmation, stale-plan rejection, safety behavior, and interrupted recovery.

A stale restore confirmation is fail-closed. The updater does not call `prepare_restore()` again merely to manufacture new authority after the journaled plan became stale.

## Rollback restart and audit receipt

Before canonical restore, the updater durably stores the `RestorePlan` fields and enters `ROLLBACK_DATA`.

If the process disappears during canonical destructive recovery, the next invocation first treats unreadable/corrupt live SQLite audit state as absence of success evidence, then calls `recover_interrupted_restore()` so the canonical restore marker/quarantine state is authoritative.

If a restore completed but the updater process disappeared before advancing its own journal, the restored database contains canonical `reliability.restore_completed` audit evidence. The updater requires that receipt to bind both:

- the operation-unique pre-update backup filename; and
- the exact pre-update backup SHA-256.

Only that bound receipt permits advancing to restart the old package without executing the restore again.

## Filesystem and platform assumptions

The install stage/rollback directories are siblings of the installation directory so replacement remains on one filesystem. Database backup/recovery follows the stronger canonical `SQLiteRecoveryManager` guarantees current at integration time.

Unicode and whitespace in installation, database, and updater-state paths are supported and covered by focused tests.

The updater's file lease is a local cross-process serialization primitive. It is not an authenticated sandbox against unrestricted same-user filesystem rewriting.

## Parallel-lane compatibility truth

ONE-SHOT-55 consumes release-integrity work from ONE-SHOT-01/#333 and does not duplicate it. Canonical SQLite WAL/cross-process restore ownership remains with ONE-SHOT-47/#311 or its integrated successor. Release database metadata adapter work remains separate from the updater state machine.

The M12 workflow is a shared release surface. Any independently integrated M12 attestation/governance changes must be converged into this lane with an explicit compatibility decision before exact-head acceptance or merge credit.

## Automated evidence and non-claims

Focused deterministic tests cover:
- successful update and repeated invocation;
- crash after backup and restart;
- corrupt package;
- wrong source SHA;
- stale/old ZIP substitution;
- outer provenance/reference mismatch;
- migration failure rollback;
- startup/health failure after package replacement;
- stale restore confirmation;
- interrupted canonical destructive restore and restart;
- Unicode/space paths.

M11 and M12 additionally exercise the final exact Windows ZIP through the update lifecycle and run the installed executable health proof.

Automated evidence does not grant human acceptance.

`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
`PRODUCTION_RELEASE_READY=false`.
