# MANUAL-DEV18 — Foundry cache evidence framing hardening

Date: 2026-08-23
Starting live main: `e40691a6e2ff9c31fd413f63d004612e048d95ed`
Branch: `work/manual-dev18/foundry-cache-evidence-v2`

## Scope

This is an independent DEV18 physical-proof integrity batch. It is not stacked on DEV18 PR #182 and does not modify Foundry provider runtime ownership/concurrency source, ModelGateway contracts, dependencies, release workflows, credentials, or model artifacts.

No model was selected, downloaded, loaded, or executed while producing this change.

## Deterministic defect

The prior optional cache digest in `scripts/prove_foundry_local.py` used this framing for each file:

`relative_path || NUL || file_bytes || NUL`

That stream is not prefix-free because arbitrary model bytes may themselves contain the delimiter and bytes that look like another relative path. Two different cache trees can therefore produce the same bytes presented to SHA-256 without finding a SHA-256 collision.

The regression fixture proves one concrete pair:

- tree A: one file `a` containing `X NUL b NUL Y`;
- tree B: file `a` containing `X` and file `b` containing `Y`.

Both serialize identically under the old v1 framing. They must not be accepted as the same model-cache integrity evidence.

A second evidence-boundary risk was filesystem indirection. A cache entry that is a symbolic link or Windows reparse point can make a proof hash bytes outside the selected model cache tree while reporting the in-tree relative name.

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
- rejects an empty cache tree as model checksum evidence;
- records file count and total bytes;
- compares file identity/size/mtime before and after reading and rejects a detected mutation during hashing.

The physical proof script delegates only its optional `--hash-model-cache` action to this helper. Its model selection, explicit-download gate, ModelGateway inference, resource evidence, ownership-safe unload/reload, and no-raw-prompt/response evidence behavior are otherwise unchanged.

## REUSE -> ADAPT -> CUSTOM(thin)

- REUSE: Python stdlib `hashlib`, `os.lstat`, `os.walk`, `stat`, `pathlib`.
- ADAPT: existing optional Foundry physical-proof cache checksum surface.
- CUSTOM(thin): only Nika-specific versioned model-cache evidence framing and fail-closed filesystem policy.

No additional hashing library, model manager, inference backend, or generic artifact framework is introduced.

## Cheap preflight

An isolated exact-content harness for the new helper/tests passed:

- focused pytest: `6 passed`;
- Python compile: PASS;
- source/test maximum line length: 96;
- the deterministic v1 ambiguity fixture produces equal legacy digests and different v2 digests.

Repository exact-head Core CI and complete applicable M12 remain authoritative. No local Ruff/full-repository GREEN is claimed because the authoring container has no Ruff installation/canonical checkout.

## Acceptance boundary

This change strengthens the future physical-model evidence collector. It does not itself provide physical Foundry acceptance.

Still required for real model acceptance on physical Windows:

- exact installed `foundry-local-sdk-winml` package/version;
- explicit model alias and exact public selected variant ID;
- human-reviewed model license reference;
- applicable cache checksum/bytes/resource evidence;
- real inference through ModelGateway;
- provider-owned unload/reload proof without disrupting another consumer.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`PHYSICAL_WINDOWS_FOUNDRY_INFERENCE_PROVEN=false`
