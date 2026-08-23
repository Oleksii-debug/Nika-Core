# MANUAL-DEV18 — Foundry cache evidence framing hardening

Date: 2026-08-23  
Original branch start main: `e40691a6e2ff9c31fd413f63d004612e048d95ed`  
Current synchronized main: `e8743566ffc673d6f8d272e88de0e027c23ab277`  
Branch: `work/manual-dev18/foundry-cache-evidence-v2`

## Scope

This is an independent DEV18 physical-proof integrity batch. It is not stacked on DEV18 PR #182 and does not modify Foundry provider runtime ownership/concurrency source, ModelGateway contracts, dependencies, release workflows, credentials, or model artifacts.

No model was selected, downloaded, loaded, or executed while producing this change.

The branch is synchronized non-force with current main. The intervening TECH02 immutable-action and DEV16 deterministic-planning integrations are file-disjoint from this Foundry cache-proof slice.

## Deterministic defects

The prior optional cache digest in `scripts/prove_foundry_local.py` used this framing for each file:

`relative_path || NUL || file_bytes || NUL`

That stream is not prefix-free because arbitrary model bytes may themselves contain the delimiter and bytes that look like another relative path. Two different cache trees can therefore produce the same bytes presented to SHA-256 without finding a SHA-256 collision.

The regression fixture proves one concrete pair:

- tree A: one file `a` containing `X NUL b NUL Y`;
- tree B: file `a` containing `X` and file `b` containing `Y`.

Both serialize identically under the old v1 framing. They must not be accepted as the same model-cache integrity evidence.

Additional evidence-boundary defects covered by the current implementation/tests:

1. symbolic links or Windows reparse points can redirect traversal outside the selected cache tree;
2. `os.walk()` enumeration errors can otherwise omit an unreadable/disappearing subtree;
3. a malicious/incorrect enumerator can yield a path lexically outside the selected root;
4. a pathname can be replaced between inventory `lstat` and file open;
5. file identity/size/mtime/ctime can change while the opened descriptor is being hashed;
6. files can be added or removed after initial enumeration.

## Repair

`foundry_cache_tree_sha256()` emits `sha256-tree-v2` evidence and binds:

1. a versioned domain-separation header;
2. exact file count;
3. length-prefixed UTF-8 relative path bytes;
4. exact file size;
5. exact file bytes.

The framing is unambiguous even when model bytes contain NULs or path-like byte sequences.

The helper also:

- canonicalizes the selected root only for containment checks while preserving relative-path identity for the digest;
- fails closed on symbolic links and Windows reparse-point attributes;
- requires every enumerated base/child to remain inside the selected root;
- accepts regular files/directories only;
- supplies an `os.walk(..., onerror=...)` failure path so enumeration errors cannot be silently omitted;
- rejects an empty cache tree as model checksum evidence;
- records file count and total bytes;
- opens each model file with `O_NOFOLLOW` where the platform exposes it;
- compares the opened descriptor identity against the pre-open `lstat` before hashing bytes;
- rechecks descriptor identity/size/mtime/ctime after hashing;
- rechecks pathname identity and root containment after descriptor hashing;
- performs a final full inventory rescan and rejects added, removed, replaced, or metadata-changed files detected while hashing.

The physical proof script delegates only its optional `--hash-model-cache` action to this helper. Its model selection, explicit-download gate, ModelGateway inference, resource evidence, ownership-safe unload/reload, and no-raw-prompt/response evidence behavior are otherwise unchanged.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE: Python stdlib `hashlib`, `os.lstat`, `os.open`/`os.fstat`/`os.read`, `os.walk`, `stat`, `pathlib`.
- ADAPT: existing optional Foundry physical-proof cache checksum surface.
- CUSTOM(thin): only Nika-specific versioned model-cache evidence framing and fail-closed filesystem/inventory policy.

No additional hashing library, model manager, inference backend, generic artifact framework, dependency, or model file is introduced.

## Regression coverage

The focused test module now covers 13 deterministic cases:

- concrete old-v1 structural ambiguity and v2 separation;
- deterministic digest independent of file creation order;
- relative-path and file-content binding;
- empty-cache rejection;
- file symlink rejection;
- directory-symlink/path-escape rejection;
- Windows reparse-attribute rejection;
- directory-enumeration error rejection;
- explicit enumerator path outside the selected root;
- pathname replacement between inventory and descriptor open;
- descriptor metadata mutation during hashing;
- final inventory rejection when a file is added;
- final inventory rejection when a file is removed.

Exact-content isolated preflight for the strengthened helper/tests: Python import/compile PASS, focused pytest `13 passed`, maximum source/test line length `96`.

Repository exact-head Core CI and complete applicable M12 remain authoritative for GREEN classification after this current-main synchronization.

## Physical proof path and authorization boundary

The existing `scripts/prove_foundry_local.py` remains the bounded collector. A valid real run requires:

- physical Windows;
- installed `foundry-local-sdk-winml` (the collector records the exact installed version);
- explicit `--model` alias;
- exact public `--model-id` selected variant identity;
- operator-supplied, human-reviewed `--model-license` evidence reference;
- optional resource ceilings and mandatory resource snapshots;
- real inference through Nika `ModelGateway`;
- provider-owned unload, reload inference, and final unload;
- optional `--hash-model-cache` using the v2 tree evidence above.

Inference itself never silently downloads a model. `--allow-download` is a separate explicit model-management authorization for the exact alias/variant/license and must not be supplied unless an authorized run has approved that acquisition.

No such model-download authorization exists in this coding cycle, so no model was acquired or executed here.

`MODEL_SELECTED=false`  
`MODEL_DOWNLOADED=false`  
`MODEL_LOADED=false`  
`MODEL_INFERENCE_EXECUTED=false`  
`PHYSICAL_WINDOWS_FOUNDRY_INFERENCE_PROVEN=false`  
`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`
