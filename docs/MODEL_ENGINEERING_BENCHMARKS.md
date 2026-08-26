# Nika Core — Model Engineering benchmark evidence

Status: F6 foundation slice. This document describes benchmark evidence collection only; it does not grant model-download, production-promotion, routing, permission, or source-mutation authority.

## Purpose

F6 requires Nika to compare no-LLM, local and cloud intelligence choices on versioned datasets using measured quality and resource evidence. The benchmark collector is deliberately narrow: it runs an exact candidate through an injected completion port, scores each response through an injected deterministic scorer, samples the existing resource observer before and after execution, and returns redacted evidence that can later be consumed by the canonical controlled-experiment/promotion layer.

The collector does not duplicate ModelGateway, M8 experiments, a resource monitor, a model downloader, or a model registry.

## Ownership and compatibility

This slice owns only `nika_core.model_benchmarks`. It consumes, but does not modify:

- `ModelRequest`, `ModelResponse`, `PrivacyClass` and `ProviderKind` from the provider-neutral ModelGateway contract;
- `ResourceObserverPort` and `ResourceSnapshot` from the existing resource subsystem.

Provider-neutral routing remains owned by the active ModelGateway lane. Foundry lifecycle/download/cache proof remains owned by its provider lane. Durable champion/challenger state and production promotion remain owned by M8 controlled experiments.

A later integration adapter may translate benchmark evidence into canonical M8 observations after the current M8 owner is integrated. This collector must not invent a second durable experiment repository.

## Benchmark identity

A `BenchmarkDataset` has an explicit `dataset_id`, `version`, and unique ordered cases. Each case contains the messages required for execution. Before running, Nika computes:

- a dataset SHA-256 over canonical UTF-8 JSON containing dataset identity and all ordered case messages;
- a case SHA-256 over canonical UTF-8 JSON containing case identity and messages.

The evidence contains only those digests, not prompt/message text. Changing the dataset content without changing its human version therefore still changes the cryptographic evidence identity instead of receiving silent equivalence credit.

A `ModelBenchmarkCandidate` pins:

- Nika candidate ID;
- exact provider ID;
- provider kind (`no_llm`, `local`, or `cloud`);
- requested model alias/name;
- privacy class and request timeout;
- optional expected response-model identity;
- optional reviewed license reference and artifact digest.

The runner always sends an explicit provider ID and an empty fallback route. A benchmark is intended to measure one candidate, not a runtime failover policy.

## Execution and redaction

For every case the runner:

1. validates the pre-execution `ResourceSnapshot`;
2. reads an injected monotonic clock;
3. calls the injected completion port with the exact provider/model and no fallback;
4. reads the monotonic clock again;
5. validates the post-execution resource snapshot;
6. fails closed if response request/provider/kind/model identity is inconsistent;
7. asks the injected scorer for a finite quality score in `[0, 1]`;
8. returns case evidence containing identities, hashes, measured latency, quality, and before/after CPU/RAM observations.

Returned response text is SHA-256 hashed for evidence binding and then omitted from the result. Prompt/message text and raw response text are never copied into benchmark evidence. The scorer necessarily receives the in-memory case and response while scoring; scorer implementations remain responsible for their own privacy behavior and must not log or persist those values unless separately authorized.

Run IDs, candidate IDs, dataset IDs, model names, license references and artifact-digest strings are evidence metadata and are stored verbatim. Callers must not put credentials, API keys, cookies, tokens or other secrets into identifier fields.

## Resource truth

The current canonical `ResourceObserverPort` exposes CPU percentage, memory percentage and available system memory. This slice records those values immediately before and after each request and validates their types/ranges before granting evidence credit.

This is **not** peak-resource telemetry, GPU telemetry, power telemetry, or a physical Windows performance certification. F6's broader GPU/power-profile goal remains open until a canonical observer provides that evidence. The collector must not label before/after samples as peak usage.

If the post-execution observer or monotonic clock fails, the model effect may already have occurred. The runner fails instead of manufacturing evidence and performs no automatic retry. Retry/reconciliation policy belongs to the caller and provider-effect authority.

## Promotion boundary

Benchmark output is evidence, not authority. This package has no API that:

- changes a default provider or model;
- downloads or deletes model artifacts;
- widens permissions or privacy policy;
- marks a candidate promoted;
- edits production source;
- writes experiment state.

Measured-winner promotion must go through canonical durable experiment/policy authority after independent qualification. Missing license/artifact evidence may be recorded as missing for measurement, but release/promotion policy may require it and must fail closed there.

## REUSE → ADAPT → CUSTOM (thin)

- **REUSE:** existing ModelGateway request/response identity and resource-observer contracts; Python standard-library hashing, JSON and monotonic clock.
- **ADAPT:** F6 versioned-dataset quality/latency/resource evidence to those stable contracts.
- **CUSTOM (thin):** F6-specific benchmark dataset/candidate/evidence dataclasses, deterministic scorer/completion/clock ports, canonical hashing, redaction and validation.
- **No new dependency** and no model artifact is bundled or downloaded.

## Acceptance for this slice

Focused automated coverage must prove:

- exact explicit provider/model requests with no fallback;
- no-LLM, local and cloud provider-kind compatibility through the same completion port;
- deterministic dataset/case hashing and changed-content separation;
- prompt/response text absent from serialized evidence;
- response provider/kind/model identity drift rejected;
- NaN, Boolean, string and out-of-range scorer results rejected;
- malformed CPU/memory evidence rejected before inference when it occurs in the pre-sample;
- backwards/non-finite monotonic time cannot create latency evidence;
- duplicate case IDs and malformed candidate timing fail closed.

Automated tests do not set `HUMAN_TESTED` or `NVDA_VERIFIED`. This backend slice has no standalone UI acceptance credit.
