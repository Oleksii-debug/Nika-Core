# Artifact Registry

Status: production implementation slice for the core service required by `docs/MASTER_SPEC.md`.

## Boundary

The Artifact Registry owns durable **metadata, identity, provenance references, and integrity
evidence** for artifacts. It does not become a second storage engine.

- Raw or large bytes remain owned by the subsystem/storage adapter that produced them.
- Existing `ContentAddressedBlobStore` remains the reusable storage implementation for research
  blobs; callers can register its digest, size, and an opaque storage reference.
- Local files can be registered only inside explicit `local_file_roots`. The registry resolves the
  target first, enforces root containment, computes SHA-256 and byte size, and persists immutable
  metadata. With no allowed roots, local file registration is disabled.
- The registry never executes an artifact and never elevates permissions.

This keeps the implementation aligned with `REUSE -> ADAPT -> CUSTOM (thin)`.

## Durable identity and replay

Artifact identity is deterministic from `(workspace_id, idempotency_key)`. A retry with the same
key and the same immutable metadata converges on the existing row. Reusing the key for different
bytes or metadata fails closed with `ArtifactConflictError`.

Artifact rows are immutable. Verification observations are append-only records. This separates
"what was registered" from "what was observed later" and makes missing/tampered files auditable.

## SQLite ownership

`artifact_registry_schema_migrations` is an independently owned ordered migration stream in the
canonical `SQLiteStore`, following the same subsystem-owned pattern already used by Media. The
Artifact Registry does not edit the reserved shared migration list.

Schema version 1 owns:

- `artifact_registry_records`
- `artifact_registry_verifications`
- indexes for workspace/kind, digest, producer, and verification history

A database with a newer Artifact Registry schema fails closed.

## Locations

Two location kinds are supported:

1. `local_file`: exact resolved local path; verification can recompute byte size and SHA-256.
2. `opaque_reference`: storage-owned reference plus caller-supplied immutable digest and size.
   Verification returns `unavailable` rather than claiming evidence it cannot obtain.

The registry rejects obvious credential material in locators, reserved secret metadata keys, and
metadata values containing credential markers. Callers must pass references to credentials, never
credential values.

Local file reads are least-privilege. `ArtifactRegistry.from_store(..., local_file_roots=(...))`
must receive one or more existing directories before `register_file()` is allowed. The source is
resolved before the containment check, so a symlink/junction that resolves outside the allowed root
does not broaden the registry's read authority.

## Verification states

- `verified`: current local bytes match registered immutable metadata.
- `missing`: the registered local file is absent.
- `mismatch`: bytes, size, or stable hashing no longer match.
- `unavailable`: bytes are owned by an opaque storage adapter and were not independently read.

Verification history is queryable by artifact ID. Records can also be located by SHA-256,
optionally scoped to a workspace, for deterministic handoff/dedup discovery.

## Acceptance evidence in this slice

Automated tests cover:

- ordered/idempotent migration and newer-schema fail-closed behavior;
- restart durability;
- retry/idempotency conflict semantics;
- concurrent replay convergence;
- Unicode and spaces in Windows-compatible paths;
- verified/tampered/missing evidence history;
- opaque-reference truthfulness;
- workspace/kind/producer filtering and digest lookup;
- rejection of obvious credential material;
- explicit local-file root authority and out-of-root denial;
- fail-closed handling of a timezone-naive injected clock.

`HUMAN_TESTED` and `NVDA_VERIFIED` are not claimed by this service slice.
