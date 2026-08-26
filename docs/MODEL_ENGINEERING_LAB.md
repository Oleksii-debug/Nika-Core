# Model Engineering Lab (F6)

Status: production foundation candidate. This document does not grant release, HUMAN_TESTED, or NVDA_VERIFIED credit.

## Product role

The Model Engineering Lab is Nika Core's controlled evaluation surface for models already reachable through the replaceable ModelGateway architecture. It exists to answer a narrow question with reproducible evidence: which provider/model/configuration is suitable for a declared workload under the same permission boundary?

It is **not** a language-model training project. In particular, the closed wrong-scope PR #495 and the separate `12-6-ai` training work are not Model Engineering Lab implementations for Nika Core.

## REUSE -> ADAPT -> CUSTOM(thin)

- **REUSE:** `nika_core.model_gateway` for provider routing, privacy classification, timeout/cancellation behavior, usage and provider latency.
- **REUSE:** `nika_core.resources.ResourceObserverPort` for the CPU/RAM snapshots that the current resource contract can prove.
- **REUSE:** `nika_core.experiments` as the only promotion/rollback authority. Model Lab does not directly change a champion/default model.
- **REUSE:** `nika_core.data.SQLiteStore` for durable local persistence rather than creating another database abstraction.
- **ADAPT:** benchmark candidates and replay cases into `StrategyRef`, `ReplayCase`, and `MetricObservation` records for the Experiment Engine.
- **CUSTOM(thin):** F6-owned benchmark identity, text-redacted evidence, model provenance/checksum binding, deterministic scoring hooks, fail-closed provider/model substitution checks, and a namespaced immutable evidence registry.

No new third-party dependency is introduced by this slice.

## Candidate identity

`ModelCandidate` binds:

- candidate ID;
- provider ID and provider kind;
- exact requested model name;
- model version/revision;
- license evidence reference;
- provenance evidence reference;
- permission fingerprint;
- local model artifact SHA-256.

Local candidates require an artifact SHA-256. Cloud/no-LLM candidates still require explicit version/revision and provenance/license evidence; Model Lab does not pretend that a cloud model exposes a downloadable artifact checksum when the provider does not provide one.

`candidate_identity_sha256()` binds the full candidate evidence set before it is represented as an Experiment Engine `StrategyRef`.

## Benchmark identity and privacy

A `BenchmarkSuite` is versioned and receives a deterministic SHA-256 over its execution-relevant definition. Prompt and reference text participate in that digest, but persisted `evidence_document()` output stores only their SHA-256 fingerprints, never their raw content or the model's raw response text.

Raw benchmark messages still exist in memory while inference is running. A caller must therefore classify each case with the existing `PrivacyClass`; the ModelGateway remains responsible for enforcing privacy routing.

Gateway exception messages are not persisted. Evidence records only the typed `ModelErrorCode`, preventing credentials or provider diagnostics from being copied into Model Lab evidence.

## Durable evidence registry

`SQLiteModelLabRepository` reuses `SQLiteStore.connection()` but owns only namespaced Model Lab tables and its own `model_lab_schema_migrations` marker. It does not edit the shared core or ProductProject migration catalogs.

The repository provides two immutable/idempotent records:

1. candidate registration, keyed by `candidate_id` and bound to `candidate_identity_sha256()`;
2. benchmark run evidence, keyed by `run_id`, bound to the registered candidate identity, suite identity, canonical JSON, and SHA-256.

Replaying the exact same candidate or run is a no-op. Reusing an ID with different evidence fails closed. Reads verify the persisted evidence SHA-256 over the exact stored UTF-8 bytes before decoding, require the document to be in the canonical serialization, and re-check the embedded candidate against the registered identity. The Windows-relevant path test uses Unicode directory names plus spaces.

This is integrity evidence, not a cryptographic trust root against an attacker who can rewrite both database contents and all digests. Higher-level backup/release trust remains owned by the existing recovery/provenance lanes.

## Execution safety

The runner is intentionally sequential and bounded:

- one candidate/provider is pinned for each run;
- fallback provider IDs are empty, so a benchmark cannot silently measure a different provider;
- response request ID, provider ID, provider kind, and model must match the candidate exactly;
- cloud execution requires explicit `allow_cloud=True`;
- the default is stop-on-first-failure to avoid accidental repeated spend or repeated failing work;
- a suite is limited to 1,000 model attempts and 20 repetitions per case;
- suite timeout is bounded to 600 seconds per request;
- cancellation is propagated rather than converted to apparent benchmark evidence;
- no model download is requested or authorized by Model Lab.

A failed or partial benchmark is never eligible for Experiment Engine observations.

## Metrics

Scorers own task-quality metrics such as `quality.exact_match`. The `model_lab.*` namespace is reserved for measurements produced by the runner. The initial foundation exposes:

- `model_lab.wall_latency_ms`;
- `model_lab.provider_latency_ms` when the provider reports it;
- `model_lab.total_tokens` when reported;
- `model_lab.cpu_percent` and `model_lab.memory_percent` when a `ResourceObserverPort` is supplied.

The current shared resource contract does not expose GPU telemetry, so this slice does **not** claim GPU measurement. That remains an explicit future compatibility decision with the Resource Manager owner rather than a duplicate resource-monitor implementation.

## Promotion boundary

Model Lab never calls `ExperimentEngine.complete()` on its own and never mutates ModelGateway defaults. It can:

1. build an `ExperimentDefinition` from champion/challenger `ModelCandidate` records and one benchmark suite;
2. aggregate complete benchmark repetitions into Experiment Engine `MetricObservation` records;
3. hand those records to the existing Experiment Engine, whose policy, guardrails, coverage checks, promotion, and rollback semantics remain authoritative.

This preserves the Full Product Vision rule that measured evaluation may inform promotion but anecdotes or benchmark code cannot silently promote a model.

## Acceptance evidence in this slice

Targeted automated tests cover:

- local model checksum requirement;
- suite digest binding to private prompt content without persisting the text;
- successful quality/resource/usage evidence;
- explicit cloud opt-in before any request is sent;
- provider substitution fail-closed behavior;
- typed gateway-error redaction;
- protection of the reserved infrastructure metric namespace;
- aggregation into Experiment Engine contracts without automatic promotion;
- candidate registry idempotency and immutable identity;
- durable benchmark round-trip and idempotent replay;
- rejection of conflicting `run_id` reuse and unregistered/changed candidate identity;
- byte-level persisted-evidence tamper detection;
- Unicode and space-containing SQLite paths.

Repository acceptance credit requires the exact branch/PR head to pass the repository CI. This document and automated tests cannot set HUMAN_TESTED or NVDA_VERIFIED.

## Deliberately not claimed by this slice

- GPU telemetry;
- model fine-tuning/PEFT/LoRA training;
- automatic downloading of model artifacts;
- automatic default-provider or champion mutation;
- user-interface/Product Journey integration;
- HUMAN_TESTED or NVDA_VERIFIED;
- release readiness for the whole Nika Core product.
