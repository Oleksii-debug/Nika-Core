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
Durable record/evidence JSON is bounded to 1 MiB before parsing or persistence, artifact metadata
is limited to 256 entries, and durable text must be valid UTF-8. Corrupted, malformed-Unicode, or
excessive payloads therefore fail closed instead of escaping through serialization or turning
durable rehydration into an unbounded resource-consumption path.

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
metadata values containing credential markers. Credential detection follows bounded layered percent
decoding so encoded secret assignments cannot bypass the durable secret fence. Callers must pass
references to credentials, never credential values.

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

## 2026-11-19 bounded integrity repair

Canonical #522 `5bc6f796ffd9e9e1bc29644d5e6edfc36fd7840c` is reused with
canonical audit inspection #526 `54246efd2fce23c3b7eb515c58ece1417d65c20d`.
The current base is accepted main `7678261754424de2c24457e76ff139f8e5678404`.
No second registry, persistence host, telemetry/runtime engine or dependency is added.

Two independently observed gaps have before-failing oracles:

- a valid version1 registry plus a negative/zero migration marker was accepted
  because only MAX(version) was read. The existing schema transaction now checks
  every exact ordered version and rejects noncontiguous history without rewriting it;
- a same-inode two-MiB source rewrite after the first read, with its size and mtime
  restored, produced a digest of bytes that never formed one stable file snapshot.
  Hashing now compares descriptor ChangeTime before/after alongside identity,
  size and mtime. It does not compare pathname ctime with descriptor ctime on
  Windows, where pathname ctime may represent birthtime.

The registration mutation oracle verifies zero published artifact records. Existing
stable-file registration, reopen, verification and denied path substitutions remain
covered. Three locked-Pydantic fixtures now accept the earlier string_unicode
rejection; one forged UNAVAILABLE receipt fixture expects the existing stronger
model-state rejection before deterministic identity. No denial is weakened.

The QA-only #547 adversarial file is executed read-only and is not integrated.
Focused combined result: 203 passed. Full source, independent review and new exact
hosted/package gates remain required. These facts do not certify a packaged artifact
browser, human NVDA, physical restart or whole-product readiness.

## 2026-11-19 signed-storage locator admission repair

This bounded successor of canonical #522 repairs a confirmed credential-material
admission gap. It reuses the existing credential-assignment validator and bounded
eight-pass percent decoder, adding the signed-storage authority families already
recognized by canonical AuditLog: AWS/GCS signature, credential and security-token
parameters, legacy access IDs, and Azure SAS `sig`. The seven direct secret
families recognized by AuditLog (AWS/GCS signature, credential, security-token
and SAS `sig`) are also reserved in artifact metadata, preventing trivial
relocation of authority into that dictionary. Generic public-signature and
access-ID metadata remain valid; the narrower query-context policy is not
expanded into an arbitrary metadata ban.

Twenty-seven admission cases exercised the public registry against temporary
SQLite stores: twenty locator/embedded-URL aliases and seven metadata-key
relocations. Together with read/retry regressions, the initial unchanged-baseline
selection produced 29 failures and 6 compatibility passes before repair.
After repair, rejection occurs before any record or verification publication.
Case/percent aliases, fragments and nested encoded URLs share the incumbent
validation path. Ordinary unsigned query parameters and opaque references still
register, reopen and replay idempotently; safe `credential_id`, `signature`, access-ID and
`signature_version` metadata remain valid. A denied request does not reserve its
idempotency key. Previously stored signed locators fail controlled readback without
silently rewriting or deleting their bytes.

Only synthetic temporary inputs are used by tests; no signed production URL,
credential or secret value is included in source or evidence. This slice adds no
registry, URL-fetching, authorization, persistence or dependency engine. It does not
certify full Section 8, a packaged evidence browser, human NVDA or release readiness.
Exact-source full verification, independent review and hosted Windows/package
gates remain required before integration.

Independent review then reproduced relocation of an entire signed URL or
authority assignment into a metadata key with an otherwise safe locator/value.
The existing bounded credential-assignment predicate now checks raw keys before
normalized reserved-name lookup. Six raw/percent-encoded key cases failed before
this repair and pass after it, publishing no record or receipt. Three ordinary
unsigned URL-key cases remain usable after reopen; public signature/access-ID
metadata retains its earlier compatibility boundary. The implementation uses the
same existing percent-decoding limit and adds no general URL ban.
