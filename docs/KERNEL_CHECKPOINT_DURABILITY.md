# Kernel Checkpoint Durability

## Scope

`nika_core.kernel.checkpoint.CheckpointService` is the small persisted checkpoint service used by the kernel task layer. It is not the Product Factory trusted-plan/checkpoint authority and must not replace or weaken that subsystem.

This contract reuses the existing SQLite `checkpoints` table and public `CheckpointService.save()` / `CheckpointService.latest()` API. No schema, dependency, permission, or approval boundary is added.

## Durable invariants

1. A successfully inserted checkpoint is ordered by SQLite insertion order for restart selection. Wall-clock `created_at` is metadata and is not restart authority. A backward system-clock adjustment therefore cannot make an older inserted checkpoint become `latest()`.
2. Checkpoint payloads are canonical UTF-8 JSON objects: object keys are sorted, insignificant whitespace is removed, Unicode is retained, and non-finite numbers (`NaN`, positive infinity, negative infinity) are rejected before any checkpoint row is written.
3. A durable read verifies the SHA-256 checksum before parsing payload bytes.
4. A durable read fails closed when payload bytes are malformed JSON, decode to a non-object value, contain a non-finite number, or do not match the canonical representation produced by the service.
5. Existing finite object payloads written by the prior service remain byte-compatible because the prior writer already used sorted keys, UTF-8 Unicode, and compact separators.

## Threat model

The SHA-256 checksum is an integrity checksum, not an authentication primitive. It detects accidental/torn payload-byte changes when the checksum is not simultaneously rewritten. It does not protect against an attacker with unrestricted write authority to the SQLite database who can replace both payload and checksum consistently.

The service deliberately does not introduce signing/HMAC authority, a second checkpoint store, a migration, or a new locking framework. Stronger trusted-plan and release/product-factory checkpoint authority remains owned by the Product Factory checkpoint subsystem.

## Recovery semantics

`latest(task_id)` selects the most recently inserted row for that task and validates that exact row. It does not silently fall back to an older checkpoint when the latest row is invalid. A corrupted latest checkpoint is therefore a visible recovery failure rather than an implicit rollback to stale state.

## Acceptance evidence

The focused regression family is `tests/test_kernel_checkpoint_durability.py` and covers:

- wall-clock rollback between sequential successful saves;
- rejection of `NaN` and infinities before durable write;
- matching-checksum non-finite and malformed durable JSON rejection;
- non-object durable payload rejection even with a matching checksum;
- non-canonical durable payload rejection even with a matching checksum;
- checksum tamper rejection;
- corrupt-newest fail-closed behavior with no stale fallback;
- Unicode/nested finite payload round-trip after reopening the service.

Repository Core CI and applicable integrated workflows remain authoritative for merge credit. `HUMAN_TESTED` and `NVDA_VERIFIED` are not established by these automated tests.
