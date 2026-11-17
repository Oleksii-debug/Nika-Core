# Plan 1 Section 4: verified persistence and recovery

Independent4А/4Б/4В and aggregateSection4 are DONE.

| Boundary | Reused implementation and evidence |
|---|---|
| Authoritative state | SQLiteStore, commit/rollback and foreign keys; incumbent schema streams, upgrade and future-schema rejection tests |
| Backup and portability | SQLite Online Backup, manifests/hashes, guarded restore into absent target, legacy adoption retaining source and durable receipts |
| Confirmation | Main plus committed WAL identity; stale-preview oracle retains later committed task data |
| Ownership | Persistent cross-process recovery file lease, native SQLite writer ownership, no-clobber publication |
| Recovery | Safety backup, quarantine, stage/marker restart and process-loss regressions; target_guard and record_audit contracts retained |
| Frozen WAL backups | Immutable read-only mode only for completed archives after coherence checks; live source/target reads preserve WAL |
| Metadata | Exact integer fields, duplicate/depth/size JSON bounds, generated path relationships and marker direct-regular ownership |
| Filesystem identity | Bound path/descriptor admission and pre/post mutation checks; Windows path↔descriptor birthtime with descriptor ctime retained |
| Negative/positive proof | Canonical restores, WAL writers, process ownership/loss, altered metadata, indirect/nonregular paths, source retention and archive sidecars |

Canonical #889 converged its existing #311 source with the narrow #1070 frozen
archive repair and current public APIs; #977 reused its own strict metadata plus
the existing #1198 marker-ownership delta. No second SQLite recovery service,
metadata format or whole-terminal replacement was introduced.

Exact1a578ff2 passed Core44054811610/M1144054811692/fullM1244054811621.
Local identical implementation tree39d9eda0:3111passed/5skipped; actual hosted
M12Windows:3109passed/7skipped; independent37metadata tests passed.
Current-source Windows gates include both the synthetic timestamp regression
and the actual recovery/package execution, keeping their evidence distinct.
Integration544700bf retained current-main+exactthreequalifiedpaths tree93988382;
the registry records all run/job/artifact and final ZIP hashes.

These are repository-controlled persistence/backup/recovery results, including
real synthetic-process loss tests, not a claim of human NVDA or whole-product
release readiness. Those final gates remain false/separate. Existing safety
backups/quarantine and preserved Git source support rollback. No credentials,
private logs, browser profiles, model weights or real production data included.
