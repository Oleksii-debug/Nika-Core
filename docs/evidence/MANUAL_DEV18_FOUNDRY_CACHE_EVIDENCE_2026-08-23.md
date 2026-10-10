# MANUAL-DEV18 — Foundry cache evidence framing hardening

Date: 2026-08-24  
Original branch start main: `e40691a6e2ff9c31fd413f63d004612e048d95ed`  
Current synchronized main: `23c7c1ce97b263b4aafa61bdcbace207b4476a3d`  
Branch: `work/manual-dev18/foundry-cache-evidence-v2`

## Scope

This is an independent DEV18 physical-proof integrity batch. It does not modify Foundry provider runtime ownership/concurrency source, shared ModelGateway contracts, dependencies, release workflows, credentials, permissions, or model artifacts.

No model was selected, downloaded, loaded, or executed while producing this change.

## Deterministic framing and filesystem defects

The prior optional cache digest used `relative_path || NUL || file_bytes || NUL`. That stream is not prefix-free because arbitrary model bytes may contain the delimiter plus bytes that look like a following path. A concrete one-file/two-file fixture produces the same legacy preimage without finding a SHA-256 collision.

The current implementation additionally protects against:

1. symbolic links and Windows reparse points;
2. silent `os.walk()` enumeration failure;
3. lexical/resolved path escape;
4. pathname replacement between inventory and open;
5. descriptor mutation while bytes are read;
6. pathname mutation after descriptor hashing;
7. final-inventory additions, removals, replacements, or metadata changes.

## `sha256-tree-v2`

`foundry_cache_tree_sha256()` binds:

- versioned domain separation;
- exact file count;
- length-prefixed UTF-8 relative path bytes;
- exact file size;
- exact file bytes;
- deterministic path order.

It accepts regular files/directories only, rejects an empty tree, rejects filesystem indirection, uses `os.walk(..., onerror=...)`, and opens files with `O_NOFOLLOW` where available.

## Windows path-stat versus descriptor-stat compatibility repair

Exact candidate `ccf9595f60e71a6ad91084da8afdd5a8fd804670` exposed a deterministic Windows-only failure after dependency consistency, Ruff, and compile had passed. Ubuntu Core passed, but Windows full pytest reported `2 failed, 1301 passed, 2 skipped, 10 warnings`. Both failures were ordinary unchanged-file positive cases and stopped at the pre-read comparison between `os.lstat(path)` and `os.fstat(descriptor)`.

The defect was architectural: one five-field tuple `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` was treated simultaneously as file-object identity and mutable metadata across two different OS query domains. Python 3.12 changed Windows pathname stat/lstat behavior and separately deprecates Windows `st_ctime`/`st_ctime_ns`; timestamp meaning and resolution are platform/filesystem dependent. A byte-identical file must not be rejected merely because pathname and open-descriptor metadata are exposed differently.

The repair does **not** remove race detection. It separates comparison domains:

- inventory pathname snapshot → immediate post-open pathname snapshot uses `lstat → lstat` with the complete identity/metadata tuple;
- opened descriptor before read → opened descriptor after read uses `fstat → fstat` with the complete tuple;
- cross-domain pathname/descriptor boundary requires a regular descriptor and exact expected size, but does not require every path-stat timestamp field to equal the descriptor-stat representation;
- final pathname state is again compared to the original pathname snapshot;
- final full inventory is re-enumerated and compared to the initial inventory.

Therefore a replacement between inventory and open is still rejected before bytes are read, an opened file changing during hashing is rejected, and a later pathname/inventory change is rejected. This is evidence integrity, not an OS sandbox against hostile arbitrary code already running with the same user authority.

A dedicated regression simulates a stable descriptor whose `st_ctime_ns` representation differs from the pathname stat domain and requires successful hashing of the unchanged bytes. A companion regression repeats the replacement-before-open attack and requires fail-closed rejection after the domain split.

## REUSE → ADAPT → CUSTOM(thin)

- **REUSE:** Python stdlib `hashlib`, `os.lstat`, `os.open`/`os.fstat`/`os.read`, `os.walk`, `stat`, `pathlib`.
- **ADAPT:** existing optional Foundry physical-proof cache checksum surface.
- **CUSTOM(thin):** versioned Nika cache-tree framing plus fail-closed filesystem/inventory policy.

No new dependency, hashing framework, model manager, inference backend, or native Windows identity framework is introduced.

## Regression coverage

The prior strengthened suite contains 13 deterministic framing/path/race cases. The Windows compatibility repair adds 2 cases:

- pathname/descriptor `st_ctime_ns` domains may differ without false-rejecting an unchanged file;
- pathname replacement between inventory and open remains rejected after the domain split.

Repository exact-head Core CI and complete applicable M12 are authoritative. Previous RED or GREEN results do not transfer to a newer candidate SHA.

## Physical proof and authorization boundary

The existing `scripts/prove_foundry_local.py` remains the bounded collector. A valid real run requires physical Windows, exact installed `foundry-local-sdk-winml`, explicit model alias, exact public selected variant ID, human-reviewed model-license evidence, resource evidence, real ModelGateway inference, provider-owned unload/reload/final unload, and applicable real cache digest evidence.

Inference never silently downloads a model. `--allow-download` is a separate explicit model-management authorization. No such authorization exists in this coding cycle.

`MODEL_SELECTED=false`  
`MODEL_ALIAS=NONE`  
`MODEL_VARIANT_ID=NONE`  
`MODEL_LICENSE=NONE`  
`REAL_MODEL_CACHE_DIGEST=NONE`  
`MODEL_DOWNLOADED=false`  
`MODEL_LOADED=false`  
`MODEL_INFERENCE_EXECUTED=false`  
`PHYSICAL_WINDOWS_FOUNDRY_INFERENCE_PROVEN=false`  
`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`
