# Model Engineering Lab

## Scope

The Nika Core Model Engineering Lab (MEL) is the evidence layer for comparing replaceable
intelligence candidates. It records benchmark suite identity, immutable per-case observations,
and deterministic rankings. It does **not** train or fine-tune models, download model weights,
change `ModelGateway` routing, or authorize production promotion.

This boundary keeps model experimentation separable from production provider authority:

`candidate -> benchmark evidence -> deterministic ranking -> human review`
`-> separate controlled change`

A MEL recommendation always has:

- `requires_human_review = true`;
- `promotion_allowed = false`.

There is intentionally no promotion method in `ModelEngineeringLab`.

## REUSE -> ADAPT -> CUSTOM (thin)

**REUSE**

- `nika_core.data.sqlite.SQLiteStore` for connection, transaction, Unicode/space path, and rollback
  behavior;
- Nika's existing immutable dataclass and fail-closed validation conventions;
- canonical JSON plus SHA-256 evidence identity patterns;
- provider/model vocabulary compatible with the existing provider-neutral Model Gateway.

**ADAPT**

- the independently-owned schema-ledger pattern used by durable product subsystems;
- immutable/idempotent repository writes and exact replay semantics;
- deterministic ranking with explicit bounded metric definitions rather than order-dependent
  normalization.

**CUSTOM (thin)**

- MEL contracts;
- one deterministic scorer/ranker;
- one SQLite repository using only MEL-owned tables;
- a small service facade and focused tests.

No new dependency is required.

## Data model

### Model candidate

A candidate identity is `(provider_id, model_id, revision)`. Its database key is the SHA-256 of
canonical identity JSON. `revision` should pin the most specific immutable model/build identity
available from the provider.

### Benchmark suite

A suite binds:

- `suite_id` and `version`;
- `dataset_sha256`;
- a non-empty set of required case IDs;
- one or more metrics with direction, positive weight, worst bound, and best bound.

For `maximize`, `best_value > worst_value`. For `minimize`, `best_value < worst_value`.
Changing data, cases, metrics, weights, or bounds under the same suite identity is an immutable
payload conflict and fails closed.

### Observation

An observation stores only:

- observation/run/suite/candidate/case identity;
- `input_sha256` and `output_sha256`;
- numeric metric values;
- a timezone-aware observation timestamp.

It has no field for raw prompt text, raw output text, credentials, cookies, tokens, or model
binaries. The caller owns any separately governed raw artifact store and passes only digests into
MEL.

### Recommendation

A recommendation contains:

- ranked complete candidates;
- explicitly excluded incomplete candidates;
- exact source observation SHA-256 values;
- an evidence SHA-256 binding suite identity, ranked results, exclusions, run identity, and source
  observations;
- mandatory human-review and no-promotion flags.

The recommendation ID is deterministic from its evidence digest.

## Deterministic scoring

Each metric value is linearly normalized between its declared worst and best bounds and clamped to
`[0, 1]`. Required cases are averaged per metric. Metric averages are combined by explicit positive
weights.

Scores are persisted as integer millionths (`0..1_000_000`) using decimal arithmetic with
round-half-even. This avoids ranking changes caused by display formatting or binary floating-point
tie noise. Equal total scores are ordered by the candidate canonical identity digest.

A candidate missing any required case is excluded and cannot win. Unexpected cases, duplicate
candidate/case evidence, or a metric-set mismatch fail closed.

## Durability and concurrency

MEL owns only these database tables:

- `model_engineering_schema_migrations`;
- `model_engineering_suites`;
- `model_engineering_observations`;
- `model_engineering_recommendations`.

It does not edit the shared Nika schema migration file.

Writes use SQLite `BEGIN IMMEDIATE` to serialize evidence mutation. Exact replays converge
idempotently. Reusing an immutable identity with different content raises `EvidenceConflictError`.
A recommendation atomically checks the exact current source-observation digest set before commit
and recomputes deterministic scoring in the repository. After recommendation persistence, the
run/suite pair is sealed and rejects late observations.

Persisted canonical payload bytes are checked against their stored SHA-256 on read. A mismatch
raises `EvidenceIntegrityError` rather than returning untrusted evidence.

## Minimal use

```python
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_engineering import ModelEngineeringLab, SQLiteModelEngineeringRepository

repository = SQLiteModelEngineeringRepository(SQLiteStore("state/nika.sqlite3"))
lab = ModelEngineeringLab(repository)
lab.initialize()
```

Then register a `BenchmarkSuite`, record `BenchmarkObservation` objects, and call
`lab.recommend(run_id, suite.key)`. Treat the result as review evidence only.

## Integration boundary

MEL must not directly edit or call mutation methods in:

- `nika_core.model_gateway`;
- `nika_core.experiments`;
- `nika_core.multi_agent`;
- `nika_core.research`;
- provider default/promotion policy;
- approval/permission policy.

A later integration can **read** a MEL recommendation and present it to an authorized product
workflow. Promotion remains a separately reviewed action with its own approval, provenance, and
rollback requirements.

## Acceptance evidence for the foundation batch

Automated tests cover:

- deterministic weighted ranking and review-only output;
- exclusion of incomplete candidates;
- exact replay idempotency and conflicting replay rejection;
- independent schema initialization and restart/reopen on a Unicode path containing spaces;
- sealing a run against late evidence;
- tamper detection for stored payload bytes;
- rejection of non-finite metrics and secret-shaped candidate identifiers;
- recommendation replay idempotency;
- rejection when the source observation set changes before recommendation commit;
- concurrent exact-replay convergence;
- absence of raw prompt/output fields from persisted observation JSON;
- repository rejection of a forged recommendation that bypasses deterministic scoring.

Automated evidence must not set `HUMAN_TESTED` or `NVDA_VERIFIED`. Those remain false until a real
human test provides that evidence. This backend foundation itself has no new user interface.
