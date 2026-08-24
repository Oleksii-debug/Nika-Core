# Windows local update and rollback

Status: ONE-SHOT-55 candidate contract. This document does not claim integration or release readiness.

## Purpose

Nika Core needs one local Windows update transaction that can move an already installed package and durable SQLite database to one explicitly authorized new release, then either prove startup/health or restore the old package and data. This is not a public auto-update service and does not discover releases.

The transaction is:

`installed old package + durable DB -> exact candidate verification -> canonical pre-update backup -> canonical ordered migration -> package replacement -> startup/health -> COMPLETED`

or, after a migration/startup failure:

`failure -> restore old package -> canonical restore preview/confirmation -> canonical data restore/recovery -> restart old package -> ROLLED_BACK`.

## Authority boundaries

The updater is deliberately thin.

**REUSE**

- `nika_core.packaging.release.verify_distributable_evidence` binds the exact candidate bytes to outer evidence, exact source SHA and artifact reference.
- `nika_core.packaging.release.verify_release_archive` is the release ZIP/manifest/path/hash authority from ONE-SHOT-01 #333/current successor.
- `SQLiteStore.initialize()` is the ordered application migration authority.
- `SQLiteRecoveryManager` is the only SQLite backup/verify/restore/interrupted-restore authority.
- canonical `reliability.restore_completed` audit evidence is the post-restore receipt used to reconcile a crash after restore side effects but before update-journal advancement.

**ADAPT**

- the verified ZIP and evidence are copied into a private operation directory before mutation, closing candidate path/byte substitution after authorization;
- the canonical pre-update `BackupArtifact` is retained under the update operation identity;
- the exact canonical `RestorePlan.confirmation_fingerprint` is persisted before destructive rollback and is reused, never silently regenerated after a stale-plan failure.

**CUSTOM (thin)**

- one OS-released per-installation updater lease;
- one atomic JSON install-operation journal outside the installed package and live database;
- same-parent directory rename/swap for package replacement and rollback;
- deterministic restart/reconciliation phases;
- `StartupHealthPort`, because startup/health semantics belong to the host application rather than to the package/recovery engines.

There is no second package manifest verifier, SQLite backup engine, migration stream, release-signing authority or network update channel.

## Trusted input

`UpdateRequest.expected_product`, `expected_version`, `expected_source_sha` and `artifact_reference` are host control-plane authorization inputs. A candidate archive cannot authorize itself by declaring a version/SHA in its own manifest. The updater checks candidate outer evidence and the embedded manifest against those exact host values.

This layer intentionally does **not** infer a release channel, compare semantic versions or decide that one version is globally "newer" than another. A valid historical ZIP is rejected when it does not match the exact target identity authorized by the host.

## Durable operation journal

The journal is stored under the configured updater state directory, not inside the install directory or SQLite database. An operation ID is derived from the normalized install path, database path and exact authorized product/version/source SHA.

Durable phases include:

- `candidate_verified`
- `backup_created`
- `migration_started`
- `migrated`
- `replacement_started`
- `replaced`
- `health_checking`
- `rollback_package`
- `rollback_data_prepared`
- `rollback_data`
- `restarting_old`
- terminal `completed`, `rolled_back`, or `blocked`.

Journal writes use a same-directory temporary file, file flush/fsync, atomic replacement, and directory fsync where the host OS supports that operation. Windows does not expose a portable Python directory-fsync primitive here, so the implementation does not claim stronger filesystem durability than the underlying Windows rename semantics provide.

Repeated invocation of the same operation resumes the durable phase or returns its terminal result. A different non-terminal operation for the same installation fails closed.

## Crash boundaries

### Crash after backup

The backup phase is journaled only after `SQLiteRecoveryManager.create_backup()` returns a canonical verified artifact. Restart verifies and reuses the same backup instead of creating a second update lineage.

### Crash during migration

`migration_started` is durable before invoking the canonical ordered migration adapter. `SQLiteStore.initialize()` is designed to be rerun against an already partially/current migrated database; a migration error switches to rollback.

### Crash during package replacement

`replacement_started` is durable before the old install directory is moved. Restart reconciles only the expected states:

- old install present, rollback absent -> retry old-package move;
- install absent, old rollback present, verified stage present -> publish candidate;
- candidate installed and old rollback present -> continue at `replaced`.

Unexpected package identities or paths block instead of overwriting evidence.

### Crash during data rollback

The exact canonical restore preview is persisted before restore. For corrupt/unrecoverable updater-mutated data, canonical `recover_interrupted_restore()` owns marker/quarantine recovery. For the crash window after a restore side effect completed but before the update journal advanced, the updater recognizes only the canonical `reliability.restore_completed` event that matches the operation-unique pre-update backup filename and SHA-256.

If recovery reports that a destructive restore was rolled back, the original persisted restore plan may be retried only if canonical stale-plan validation still accepts it.

## Failure policy

- corrupt ZIP, file/hash/path violation, wrong source SHA, stale historical ZIP, wrong artifact reference or other provenance mismatch: reject before backup/migration/replacement;
- migration failure: old package remains (or is restored if necessary), then data rolls back from the exact pre-update canonical backup and old startup/health must pass;
- startup/health failure after replacement: failed candidate directory is retained as evidence, old package is restored, data rolls back, then old startup/health must pass;
- stale rollback confirmation: transition to `blocked`; never generate a fresh confirmation automatically;
- canonical rollback error or missing rollback evidence: `blocked`; no destructive best-effort overwrite.

The pre-update backup and old package rollback directory are intentionally retained after a successful update. Cleanup/retention policy is a separate product decision; deleting rollback evidence inside the critical transaction would weaken recovery.

## Process and filesystem preconditions

The updater must execute from a host/updater process outside the installation directory being replaced. The application must not retain package-file handles that prevent directory rename. Canonical SQLite recovery process/lease rules still apply. ONE-SHOT-47 #311/current successor owns stronger cross-process SQLite/WAL recovery semantics; this lane consumes that public recovery authority after integration rather than copying it.

DEV29 #218/current successor remains the separate release-database metadata adapter. This local updater does not duplicate its release snapshot format.

## Windows and path support

The operation uses `pathlib`/argument arrays and does not invoke a shell for package replacement. Focused tests use Cyrillic and space-containing install/data/state paths. M11/M12 packaged proof also creates its fixture under a Unicode/space path.

## Exact artifact evidence

`scripts/m11_m12_update_lifecycle.py` performs a packaged success proof with a deterministic old-package fixture and a schema-v1 durable SQLite fixture.

For M11, the script binds the exact ZIP produced by that exact checkout to local M11 evidence, then installs it, migrates the fixture DB, verifies the canonical backup and starts the installed `NikaCore.exe --pf11-proof`. `m11-update-lifecycle-evidence.json` is uploaded with the M11 artifact.

For M12, the same proof consumes `m12-prehuman-evidence.json` for the exact M12 ZIP and emits `m12-update-lifecycle-evidence.json`, uploaded in the same pre-human evidence artifact. The proof marks the old package as a fixture and never represents it as a historical production build.

Automated evidence always records:

- `human_tested=false`
- `nvda_verified=false`
- `production_release_ready=false`.

Physical human NVDA acceptance remains a separate M12 human gate.
