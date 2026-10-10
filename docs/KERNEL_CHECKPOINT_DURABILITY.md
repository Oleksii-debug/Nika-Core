# Kernel Checkpoint Durability

## Scope

`nika_core.kernel.checkpoint.CheckpointService` is the small persisted checkpoint service used by the kernel task layer. It is not the Product Factory trusted-plan/checkpoint authority and must not replace or weaken that subsystem.

This contract reuses the existing SQLite `checkpoints` table and public `CheckpointService.save()` / `CheckpointService.latest()` API. No schema, dependency, permission, or approval boundary is added.

## Durable invariants

1. A successfully inserted checkpoint is ordered by SQLite insertion order for restart selection. Wall-clock `created_at` is metadata and is not restart authority. A backward system-clock adjustment therefore cannot make an older inserted checkpoint become `latest()`.
2. Checkpoint payloads are canonical UTF-8 JSON objects: object keys are sorted, insignificant whitespace is removed, Unicode is retained, and non-finite numbers (`NaN`, positive infinity, negative infinity) are rejected before any checkpoint row is written.
3. Durable `payload_json` and `checksum_sha256` carriers must be real SQLite TEXT values. SQLite affinity does not prevent a corrupt row from storing BLOB or other storage classes, so restart reads reject those carriers before hashing or parsing.
4. A durable read verifies the SHA-256 checksum before parsing payload bytes.
5. A durable read fails closed when payload bytes are malformed JSON, recurse beyond the JSON decoder/encoder safety boundary, decode to a non-object value, contain a non-finite number, or do not match the canonical representation produced by the service.
6. Checkpoint payloads are bounded before serialization to at most 10,000 JSON value nodes, depth 64, 4,096-bit integers, and 1 MiB canonical UTF-8 JSON. Container admission requires exact built-in `dict`, `list`, or `tuple` carriers before length/iteration hooks run; JSON scalar values and scalar keys cannot use behavioral `str`, `int`, or `float` subclasses to bypass byte/bit accounting; invalid Unicode fails before SQLite mutation.
7. Restart reads fetch at most the admitted payload prefix and 65 checksum bytes through SQLite BLOB projections, verify the underlying storage classes and byte lengths, then strictly decode UTF-8/ASCII. Oversized durable payloads or checksums therefore fail closed without returning their full TEXT values to Python.
8. Public `task_id` and `stage` inputs are exact strings with valid UTF-8 and a 4 KiB byte ceiling. Restart selection considers byte-identical non-TEXT `task_id` aliases in insertion order, so a corrupted newest identity cannot disappear from lookup and expose an older checkpoint. Bounded SQLite projections then reject non-TEXT durable `checkpoint_id`, `task_id`, and `stage` carriers and bind the persisted task identity to the requested task before exposing a `Checkpoint`.
9. Existing finite object payloads written by the prior service remain byte-compatible because the prior writer already used sorted keys, UTF-8 Unicode, and compact separators.

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
- Unicode/nested finite payload round-trip after reopening the service;
- BLOB `payload_json` and BLOB checksum storage-class rejection;
- controlled rejection of excessive JSON nesting on both write and restart read;
- pre-serialization node/depth/integer/UTF-8 resource admission with exact positive boundary cases;
- behavioral container subclasses rejected before their length/iteration hooks can bypass admission;
- scalar/key subclasses rejected before `json.dumps`, preserving byte and integer-bit preflight bounds;
- oversized durable payload/checksum rejection through bounded SQLite BLOB projections;
- durable node-overflow rejection even when checksum and canonical JSON bytes otherwise match;
- exact-string and UTF-8/byte-bounded task/stage ingress;
- BLOB/oversized durable checkpoint identity and stage rejection before public rehydration.
- byte-identical BLOB `task_id` aliases fail closed instead of returning an older checkpoint or `None`.

Repository Core CI and applicable integrated workflows remain authoritative for merge credit. `HUMAN_TESTED` and `NVDA_VERIFIED` are not established by these automated tests.
