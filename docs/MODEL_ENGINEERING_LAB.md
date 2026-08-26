# Model Engineering Lab

Status: backend evaluation foundation. This document does not claim a finished UI/Product Journey.

## Purpose

The Model Engineering Lab is the Nika-owned layer for comparing replaceable intelligence
components on versioned evidence before any production activation. It supports local,
local-server and allowed cloud candidates without granting the lab authority to change
production model routing.

The first integrated slice is deliberately narrow:

1. bind model/provider provenance to immutable candidate manifests;
2. bind evaluation cases to versioned dataset references and SHA-256 evidence;
3. adapt model evaluations to the existing durable `nika_core.experiments` engine;
4. record deterministic quality/resource/latency/cost metrics supplied by the evaluator;
5. return a recommendation and evidence digest;
6. require the normal product approval/configuration path for any later activation.

Raw evaluation prompts, expected answers and model responses are not fields in this layer and
are not persisted by it. The dataset/corpus owner remains responsible for the actual content.
References in manifests must be non-secret references; credentials, tokens and signed URLs do
not belong in candidate or dataset metadata.

## REUSE -> ADAPT -> CUSTOM (thin)

### REUSE

- `nika_core.experiments.ExperimentEngine` remains the champion/challenger decision authority.
- `SQLiteExperimentRepository` remains the durable SQLite evidence store.
- `MetricRule`, `PromotionPolicy`, append-only observations and rollback semantics are reused.
- `ProviderKind` and `PrivacyClass` are reused from ModelGateway contracts.

### ADAPT

`ModelEngineeringLab` maps:

- `ModelCandidate` -> `StrategyRef(artifact_kind=CONFIG)`;
- `EvaluationCase` -> `ReplayCase`;
- `CaseMeasurement` -> the existing `MetricObservation` records;
- model policy -> the existing `PromotionPolicy` and guardrails.

Candidate and case metadata are encoded into deterministic manifest references and SHA-256
bound to the existing immutable experiment definition. This avoids adding a second database
schema only for Model Engineering metadata.

### CUSTOM (thin)

Only model-domain validation is custom:

- provider/model/source/license/checksum identity;
- permission-fingerprint equality across all compared candidates;
- private-data capability checks against evaluation privacy;
- exact metric-set validation;
- idempotent measurement replay and partial-write resume;
- deterministic recommendation evidence digest;
- explicit no-auto-activation result boundary.

## Candidate identity

A candidate contains only non-secret identity/evidence metadata:

- candidate ID;
- provider ID and provider kind;
- exact model ID and model version;
- non-secret source/provenance reference;
- license evidence reference;
- permission fingerprint;
- whether reviewed provider evidence allows private data;
- optional lowercase SHA-256 artifact checksum for downloadable/local artifacts.

The canonical JSON form is SHA-256 hashed. That digest becomes the underlying experiment
`StrategyRef.version`. Any persisted manifest/digest mismatch fails closed when evidence is
read or when a measurement is recorded.

`artifact_sha256=None` is allowed for provider-hosted models where no downloadable immutable
artifact exists. The exact provider model identity/version and source/license evidence remain
mandatory.

## Evaluation identity and privacy

An evaluation case contains:

- case ID;
- non-secret dataset/corpus reference;
- human dataset version;
- SHA-256 for the immutable evaluation-set artifact or manifest;
- `PrivacyClass`.

The case manifest is hashed and stored through the existing `ReplayCase` definition. The lab
never stores raw prompt/response text.

A non-public case is rejected before experiment creation if any compared candidate lacks
reviewed `supports_private_data` evidence. This is a fail-closed precondition; it does not
replace ModelGateway's runtime routing/privacy enforcement.

## Metrics and promotion policy

Metric names are intentionally provider-neutral. A caller may use, for example:

- task success or quality score as the primary metric;
- latency in milliseconds;
- peak memory/RAM;
- CPU/GPU utilization;
- normalized cost;
- deterministic failure rate or tool-success score.

Each experiment declares exactly one primary metric and zero or more guardrails. Every
candidate/case measurement must provide exactly that metric set. Missing or undeclared metrics
are rejected before writing any part of that measurement.

The existing Experiment Engine requires full replay coverage and applies:

- minimum primary-metric improvement;
- metric direction (`higher_is_better`);
- maximum allowed guardrail regression;
- deterministic tie breaking.

## Durability, replay and recovery

The lab does not create a second persistence engine. With `SQLiteExperimentRepository`, the
existing experiment tables provide restart durability.

Operations are designed for uncertain replay:

- repeating `create` with the identical immutable definition returns the existing experiment;
- repeating `start` while already running is idempotent;
- repeating the same metric evidence with the same value is idempotent;
- a conflicting value for an already-recorded candidate/case/metric key fails closed;
- if a process stops after only some metrics of one measurement were persisted, repeating the
  complete `CaseMeasurement` verifies existing values and records only missing metrics;
- repeating `complete` after a successful completion returns the same recommendation;
- repeating rollback after a completed rollback returns the same rollback recommendation.

This layer does not claim arbitrary distributed exactly-once execution. Concurrent writers are
bounded by the Experiment Repository's append-only/SQLite transaction semantics; a short
bounded reconciliation is used for a racing metric write and fails rather than silently
choosing conflicting evidence.

## Recommendation and activation boundary

`ModelRecommendation` includes:

- experiment ID;
- selected candidate ID;
- selected candidate manifest SHA-256;
- deterministic aggregate evidence SHA-256;
- previous champion ID;
- `requires_activation_approval=True` (not caller-settable);
- `production_mutation_performed=False` (not caller-settable).

There is intentionally no `activate`, `write_config`, ModelGateway mutation, deployment or
source-rewrite method in `ModelEngineeringLab`.

A future integration may consume the recommendation through the normal Nika approval,
configuration and release path. That integration must preserve the existing R0-R4 boundaries,
permission ceiling, provenance evidence and rollback authority. Model Engineering evidence by
itself is never an approval.

## Current API sketch

```python
store = SQLiteStore("nika.sqlite3")
store.initialize()
lab = ModelEngineeringLab(SQLiteExperimentRepository(store))

snapshot = lab.create(spec)
lab.start(snapshot.definition.experiment_id)
lab.record_measurement(snapshot.definition.experiment_id, measurement)
recommendation = lab.complete(snapshot.definition.experiment_id)
```

The evaluator that actually invokes models is intentionally outside this first slice. It may
later be a thin adapter over ModelGateway, Foundry Local, Ollama/local-server or another allowed
provider, but it must not duplicate provider routing and must respect the current owner of those
surfaces.

## Acceptance evidence for this slice

Focused tests cover:

- immutable candidate and evaluation manifest binding;
- tamper detection;
- permission widening rejection;
- private evaluation vs candidate capability rejection;
- exact metric-set validation before partial write;
- idempotent same-value evidence replay;
- immutable conflicting evidence;
- recovery after a partially recorded multi-metric measurement;
- promotion when primary improvement and guardrails pass;
- champion retention when a guardrail regresses too far;
- exact recommendation equality after SQLite repository restart;
- idempotent create/start/complete replay.

Required repository gates remain Core CI and M12 on the exact branch head. Windows/M11 is only
credited when the relevant workflow runs on that exact head. This backend slice is not a human
accessibility proof.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

## Explicit non-scope / ownership boundaries

This slice does not edit:

- ModelGateway provider/routing implementation or contracts owned by the active DEV17 lane;
- Foundry/embedded model provider implementation;
- media/ASR model internals;
- Experiment Engine contracts, engine or repository;
- Product Factory deployment/configuration;
- Windows UI/Interaction surfaces.

Any future shared-contract change requires a recorded compatibility decision before editing the
other lane's files.

## Next coherent batch

After this foundation is exact-green and integrated, the next independent Model Engineering
batch should add a thin evaluator adapter that invokes current public ModelGateway contracts,
captures measured latency/resource/usage evidence, and feeds `CaseMeasurement`. It must first
reread live main and open PR ownership because ModelGateway is an actively changing shared
surface.

A later UI/Product Journey batch can expose accessible keyboard/NVDA-first experiment creation,
progress, comparison and recommendation review. It must use standard semantic controls and must
not claim `NVDA_VERIFIED` until a human NVDA run exists.
