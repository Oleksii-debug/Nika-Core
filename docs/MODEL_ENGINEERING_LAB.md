# Nika Model Engineering Lab

Status: implementation foundation for the Full Product Vision Model Engineering Lab.
Starting main: `109829579ab4693e038e218769c23c2547defd64`.

## Product boundary

The Model Engineering Lab evaluates and manages replaceable intelligence components already exposed through Nika `ModelGateway`. It is not a foundation-model training project and it does not own provider SDKs, model downloads, credentials, permission policy, or production source mutation.

The first slice compares a declared champion with one or more challengers on a versioned replay suite, records only declared numeric metrics through the existing M8 `ExperimentEngine`, and lets the existing promotion/rollback policy decide the winner. A benchmark never grants a provider, model, candidate, or evaluator more permission than the experiment champion.

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE** `ModelGateway` for provider selection, privacy routing, timeout behavior, audit, usage and provider responses.
- **REUSE** M8 `ExperimentEngine`, `ExperimentRepository`, immutable definitions, append-only observations, promotion guardrails and rollback.
- **REUSE** ordinary Python hashing, monotonic time and deterministic arithmetic. No new dependency is introduced.
- **ADAPT** a thin `ModelEngineeringLab` orchestration service that maps a benchmark candidate/case into existing `StrategyRef`, `ReplayCase` and `MetricObservation` contracts.
- **CUSTOM (thin)** Model-Lab-specific candidate/case identity, suite fingerprinting, response-identity checks and a deterministic exact-text baseline evaluator because these semantics are specific to reproducible Nika model evaluation.

No new orchestration kernel, experiment database, model gateway, scheduler, model runtime, vector database or generic evaluation framework is added.

## Contracts

`BenchmarkCandidate` binds:

- stable candidate ID;
- exact provider ID;
- requested model alias/ID;
- expected response model identity;
- candidate/config version;
- provenance/artifact reference;
- permission fingerprint.

`BenchmarkCase` binds:

- stable case ID;
- dataset/provenance reference and version;
- exact `ModelMessage` sequence;
- deterministic evaluator reference data;
- Nika privacy class;
- timeout.

`BenchmarkPlan` binds champion, challengers, cases, M8 `PromotionPolicy`, temperature and non-secret benchmark metadata.

## Immutable identity and raw-data minimization

The existing M8 experiment definition must be enough to reject silent benchmark drift without storing the raw prompt or expected answer as experiment evidence.

For each candidate, the Model Lab persists the ordinary candidate artifact reference plus a SHA-256 identity digest that binds provider ID, requested model, expected response model, candidate version and artifact reference.

For each replay case, the persisted `dataset_version` includes a SHA-256 digest that binds:

- message roles and content;
- reference data;
- privacy class;
- timeout;
- temperature;
- benchmark metadata;
- evaluator ID and evaluator version.

The raw prompt, response and reference text are not written to M8 `ExperimentDefinition` or `MetricObservation`. The caller remains responsible for keeping the versioned evaluation dataset available through its declared dataset reference. Benchmark metadata and artifact references must not contain secrets.

Changing candidate identity, case content, evaluator version, request policy or promotion policy under the same `benchmark_id` changes the immutable M8 definition and fails with `BenchmarkDefinitionMismatchError` rather than silently mixing evidence.

## Execution semantics

For every candidate and replay case the service:

1. creates a `ModelRequest` with explicit `provider_id`, exact model, case privacy, timeout and temperature;
2. supplies no fallback provider, preventing a benchmark candidate from being silently substituted by a fallback route;
3. executes through the ordinary `ModelGateway`, so sensitive-data routing and provider timeout policy remain authoritative;
4. verifies that the returned provider ID and model identity exactly match the declared candidate;
5. measures gateway wall latency with a monotonic clock;
6. obtains deterministic evaluator metrics and optional provider usage/latency metrics;
7. requires every primary/guardrail metric declared by the M8 policy to exist and be finite;
8. records only those declared observations through `ExperimentEngine`;
9. calls the existing M8 completion logic only after full candidate/replay coverage exists.

Built-in system metric names are:

- `gateway_latency_ms`;
- `provider_latency_ms` when supplied by the provider;
- `input_tokens`, `output_tokens`, `total_tokens` when supplied by the provider.

The first deterministic evaluator is `ExactTextMatchEvaluator` (`text.exact_match`, version `1`), normally emitting `exact_match` as `1.0` or `0.0`. More evaluators can be added as narrow adapters with explicit version identity; an LLM judge is not required for the baseline and must never invent permission or acceptance evidence.

## Restart and partial-evidence rule

M8 supports append-only partial experiments. Model outputs can be nondeterministic, so restart must not silently combine metrics from two different responses for one candidate/case.

On rerun with the same immutable benchmark definition:

- a candidate/case with **all** required metrics already present is reused and the model is not called again;
- a candidate/case with **none** of the required metrics is safe to execute;
- a candidate/case with only **some** required metrics is treated as torn evidence and fails closed with `BenchmarkEvidenceIntegrityError`.

This intentionally prefers an explicit new benchmark/evidence repair over laundering two generations into one replay record. A future atomic multi-metric evidence transaction may relax this only with an explicit shared-contract compatibility decision in M8.

A benchmark already in completed/promoted/rolled-back state is idempotently returned without re-executing providers. Model-Lab rollback delegates to the ordinary M8 rollback transition and therefore works only after a recorded promotion.

## Security and privacy

- Model Lab has no model-download authorization path.
- It does not access credentials directly.
- It does not bypass ModelGateway privacy routing.
- It supplies no automatic fallback route during candidate measurement.
- It does not execute tools or high-impact actions.
- It does not edit production source or expand permissions.
- It does not persist raw model responses as experiment evidence.
- Provider/model response substitution fails before metric evidence is recorded.
- Missing/non-finite required metrics fail before evidence is recorded.

## Acceptance for this foundation

Source-level acceptance requires focused regressions proving:

1. a better challenger can be promoted under quality plus latency guardrail evidence;
2. rollback restores the recorded champion through M8;
3. restart reuses fully recorded case evidence instead of re-running it;
4. torn multi-metric case evidence fails before another model response is mixed in;
5. prompts/reference text are bound by digest but absent from immutable experiment prose/evidence;
6. changed temperature/evaluator identity under one benchmark ID is rejected as immutable-definition drift;
7. provider/model identity substitution fails before observation recording;
8. sensitive-data policy remains enforced by `ModelGateway`;
9. a required metric that the provider/evaluator cannot produce fails closed.

Repository-wide dependency consistency, Ruff, compile and full pytest must pass on the exact branch head, followed by the normal exact-head Core CI. This foundation changes no dependency, schema, workflow, package, UI or Windows-specific runtime surface, so it does not by itself prove packaged Product Journey, physical Foundry performance, RAM/CPU/GPU instrumentation, HUMAN_TESTED, NVDA_VERIFIED or production release readiness.

## Next coherent Model Lab batches

After this foundation is integrated and exact-green, subsequent non-overlapping batches should add only when backed by a concrete acceptance need:

- resource-observer adapters for CPU/RAM and, where proven available, GPU metrics;
- cost normalization for cloud providers without storing credentials or raw content;
- versioned benchmark-suite loading/provenance from the research/corpus layer;
- specialist deterministic evaluators for structured output, retrieval and task success;
- optional measured DSPy/PEFT experiment adapters only behind M8 and only with explicit datasets/metrics;
- accessible Model Lab workspace/presentation after the backend contract is integrated and a UI owner coordinates the Product Journey slice;
- physical Windows/Ollama/Foundry benchmark evidence on exact installed provider/model artifacts.

These later capabilities must continue to use ModelGateway and M8 rather than creating competing provider or experiment frameworks.

`HUMAN_TESTED=false`
`NVDA_VERIFIED=false`
`PRODUCTION_RELEASE_READY=false`
