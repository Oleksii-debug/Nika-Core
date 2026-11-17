# Backup/restore audit continuity

## Scope

This note extends the DEV28 backup/restore reliability batch in
`docs/BACKUP_RESTORE_WAL_AUTHORITY.md`. It does not modify R0-R4 policy, approval
provenance, credentials, schema migrations, or the active M10 PR #61/#62 ownership.

## Defect

For a healthy live database, `SQLiteRecoveryManager.restore()` creates a safety backup
before replacing the live database. The original path called public `create_backup()`.
That method first copied the live database into the safety artifact and then appended a
`reliability.backup_created` event to the live database.

That event had two bad properties:

1. it was written after restore preview/confirmation had already been revalidated, so the
   restore implementation itself mutated the approved live state before replacement;
2. it was not in the safety backup because the copy happened before the audit append, and
   it disappeared from the final database when the staged restore replaced the live file.

The result was intentionally generated audit evidence with no durable surviving copy.

## Repair

Public `create_backup()` keeps its existing audited behavior. The implementation now
uses a private `_create_backup(..., record_audit=...)` helper. Ordinary external backups
call it with `record_audit=True`.

The restore-only safety backup calls the same copy/verify/manifest implementation with
`record_audit=False`. This prevents an internal post-confirmation mutation of the live
database and avoids generating an audit event that is immediately discarded.

The durable final restored database already receives `reliability.restore_completed` on
the staged database before publication. That event records `safety_backup_file` together
with the restore source evidence, so the internal safety artifact remains represented in
the surviving restore audit trail.

No backup bytes, manifest fields, rollback behavior, quarantine behavior, permission,
approval, or external backup audit contract are weakened.

## Focused evidence

`tests/test_backup_restore_audit_continuity.py` uses a recording audit adapter around the
real `AuditLog` and proves that a healthy restore:

- still creates a safety backup;
- emits no transient live `backup_created` event from the internal safety operation;
- preserves a final `reliability.restore_completed` event;
- binds that event to the exact generated safety-backup filename.

Existing backup/restore crash, corruption, WAL and rollback suites remain broad regression
authority. Exact-head Core CI on Ubuntu and Windows plus applicable M12 evidence are
required before acceptance.

## REUSE -> ADAPT -> CUSTOM

- REUSE the existing SQLite online-backup, manifest and restore evidence machinery.
- ADAPT one existing private implementation path so internal and external backup audit
  semantics can differ without duplicating the backup algorithm.
- CUSTOM remains thin Nika recovery/audit policy only.

No new dependency, migration, permission, secret material, or approval level is added.

## Residual concurrency boundary

This repair does not claim a universal cross-process quiescence lock. The separate WAL
state commitment detects changes present at revalidation, while a repository-wide writer
lease covering every direct SQLite client would require a shared-contract compatibility
decision outside this isolated DEV28 batch.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.
