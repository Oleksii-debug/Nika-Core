# MANUAL-DEV18 — Foundry cache evidence framing hardening

Date: 2026-08-23  
Original branch start main: `e40691a6e2ff9c31fd413f63d004612e048d95ed`  
Current synchronized main: `8e2e0eb3f0f65b75e1d23b0f36ab2bf09a8477ba`  
Branch: `work/manual-dev18/foundry-cache-evidence-v2`

## Scope

This is an independent DEV18 physical-proof integrity batch. It is not stacked on DEV18 PR #182 and does not modify Foundry provider runtime ownership/concurrency source, ModelGateway contracts, dependencies, release workflows, credentials, or model artifacts.

No model was selected, downloaded, loaded, or executed while producing this change.

The branch was synchronized non-force with the current main after DEV06 Product Factory integration. That main movement touched only DEV06 Product Factory/docs/tests and had no Foundry cache-proof overlap.

## Deterministic defects

The prior optional cache digest in `scripts/prove_foundry_local.py` used this framing for each file:

`relative_path || NUL || file_bytes || NUL`

That stream is not prefix-free because arbitrary model bytes may themselves contain the delimiter and bytes that look like another relative path. Two different cache trees can therefore produce the same bytes presented to SHA-256 without finding a SHA-256 collision.

The regression fixture proves one concrete pair:

- tree A: one file `a` containing `X NUL b NUL Y`;
- tree B: file `a` containing `X` and file `b` containing `Y`.

Both serialize identically under the old v1 framing. They must not be accepted as the same model-cache integrity evidence.

Additional evidence-boundary defects found during review:

1. A cache entry that is a symbolic link or Windows reparse point can make a proof hash bytes outside the selected model cache tree while reporting the in-tree relative name.
2. Python `os.walk()` ignores directory enumeration errors unless an `onerror` callback is supplied. An unreadable/disappearing subtree could therefore be silently omitted from a checksum.
3. A file could be added or removed after initial enumeration while hashing was in progress, yielding evidence for an incomplete inventory unless the tree is reconciled again before success.

## Repair

`foundry_cache_tree_sha256()` now emits `sha256-tree-v2` evidence and binds:

1. a versioned domain-separation header;
2. exact file count;
3. length-prefixed UTF-8 relative path bytes;
4. exact file size;
5. exact file bytes.

The framing is unambiguous even when model bytes contain NULs or path-like byte sequences.

The helper also:

- fails closed on symbolic links and Windows reparse-point attributes;
- accepts regular files/directories only;
- supplies an `os.walk(..., onerror=...)` failure path so enumeration errors cannot be silently omitted;
- rejects an empty cache tree as model checksum evidence;
- records file count and total bytes;
- compares file identity, size, modification time, and change time before/after each read;
- performs a final full inventory rescan and rejects added, removed, replaced, or metadata-changed files detected while hashing.

The physical proof script delegates only its optional `--hash-model-cache` action to this helper. Its model selection, explicit-download gate, ModelGateway inference, resource evidence, ownership-safe unload/reload, and no-raw-prompt/response evidence behavior are otherwise unchanged.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE: Python stdlib `hashlib`, `os.lstat`, `os.walk`, `stat`, `pathlib`.
- ADAPT: existing optional Foundry physical-proof cache checksum surface.
- CUSTOM(thin): only Nika-specific versioned model-cache evidence framing and fail-closed filesystem/inventory policy.

No additional hashing library, model manager, inference backend, generic artifact framework, dependency, or model file is introduced.

## Regression coverage

The test module covers:

- concrete old-v1 structural ambiguity and v2 separation;
- deterministic digest independent of file creation order;
- relative-path and file-content binding;
- empty-cache rejection;
- symbolic-link rejection;
- Windows reparse-attribute rejection;
- directory-enumeration error rejection;
- final inventory rejection when a new file appears during hashing.

An initial isolated exact-content harness, before the final two inventory fail-closed cases were added, passed `6` focused tests plus Python compile and had maximum source/test line length `96`. The final candidate receives no GREEN credit from that partial local harness. Repository exact-head Core CI and applicable M12 are authoritative for the complete current source/tests.

## Acceptance boundary

This change strengthens the future physical-model evidence collector. It does not itself provide physical Foundry acceptance.

Still required for real model acceptance on physical Windows:

- exact installed `foundry-local-sdk-winml` package/version;
- explicit model alias and exact public selected variant ID;
- human-reviewed model license reference;
- applicable real cache checksum/bytes/resource evidence;
- real inference through ModelGateway;
- provider-owned unload/reload proof without disrupting another consumer.

`MODEL_SELECTED=false`  
`MODEL_DOWNLOADED=false`  
`MODEL_LOADED=false`  
`MODEL_INFERENCE_EXECUTED=false`  
`PHYSICAL_WINDOWS_FOUNDRY_INFERENCE_PROVEN=false`  
`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`
