# Nika Core — intelligence reuse decisions

Updated: 2026-08-19.
Status: binding addendum to `docs/REUSE_CATALOG_2026-08-18.md` for intelligence/model/planning choices. Where this file conflicts with the older catalog on these topics, this file is newer.

## Decision order

REUSE -> ADAPT -> CUSTOM (thin). Nika owns contracts, permissions, product semantics and evidence; upstream engines remain replaceable.

## A. Deterministic Brain — zero model

### Unified Planning 1.3.x + Pyperplan — ADAPT
- Project: AIPlan4EU Unified Planning.
- License: Apache-2.0.
- Role: formal planning for explicit goals/actions/preconditions/effects.
- Integration: optional `planning` dependency; Nika `WorldState`, goal/action and planner contracts remain framework-neutral.
- First proof engine: Pyperplan through Unified Planning.
- Non-goal: do not convert every open-ended natural-language task into a planning domain.
- Security: plans call ordinary Nika tools; ToolExecutor permissions/approval remain authoritative.
- Execution budget: Nika bounds planner wall time and rejects a returned plan whose step count exceeds the caller's explicit `max_steps` budget before any tool action is executed. Planning remains cancellable at the asyncio caller boundary; a timed-out worker thread is not falsely represented as a hard-killed native planner process.

### SQLite FTS5 / deterministic search — REUSE
Use for local corpus/source search before semantic/vector retrieval. Preserve provenance and workspace scopes.

### RapidFuzz — REUSE when a concrete dedup/search workflow requires it
Use for fuzzy entity/title comparison. Do not add to mandatory core until a real workflow needs it.

### scikit-learn — REUSE per measured task
Use for classification/ranking/clustering/regression only with explicit data and metrics.

### Experiment Engine — REUSE Nika integrated subsystem
Use versioned metrics/champion-challenger evidence to select deterministic strategies. No autonomous production-source mutation.

## B. Embedded Brain — local generative model without Ollama/API

### Microsoft Foundry Local 1.2.3 — PRIMARY ADAPT candidate/integration
- Official repository: `microsoft/Foundry-Local`.
- SDK license: MIT; each downloadable model has its own license and must be audited separately.
- Windows package: `foundry-local-sdk-winml>=1.2.3,<2`.
- Cross-platform package where needed: `foundry-local-sdk>=1.2.3,<2`.
- The two SDK package variants are mutually exclusive in one Python environment.
- Use the official Python SDK directly for embedded/in-process inference; the optional local web service is not required for normal Nika embedding.
- Nika adapter: `FoundryLocalProvider` behind `ModelProvider` / ModelGateway.
- Privacy: provider is LOCAL and can keep private/sensitive input on-device under Nika routing policy.
- Model management: no silent model download. Models are optional components outside the base EXE and require explicit install/download intent, version/license/checksum/resource evidence.
- Resource policy: Microsoft positions Foundry Local as single-user on-device inference rather than a concurrent server stack, so Nika serializes in-process completions per provider instance instead of pretending it has server-style batching/queueing. The existing `ModelRequest.timeout_seconds` bounds time spent waiting for that slot plus the user-visible inference wait.
- Hardware: Windows WinML package is preferred for actual Windows hardware; final acceptance requires a physical-Windows inference proof.
- Cancellation truth: current official Python docs explicitly expose cancellation for model/EP downloads. They do not document a hard-cancel primitive for an active non-streaming inference. Nika returns typed timeout/cancellation semantics at its async boundary but must not claim the underlying native inference thread was hard-killed until a focused proof or process-isolation design closes that gap.

### llama.cpp — FALLBACK / PROOF CANDIDATE
- Role: alternative embedded generative backend where GGUF model availability, CPU/Vulkan portability, performance or packaging wins a measured Nika benchmark.
- Integration: maintained binding/native adapter behind ModelGateway; do not vendor the repository wholesale.
- Adoption gate: exact binding/version/license, Windows x64 CPU/AMD path, cancellation behavior, model lifecycle, package size, memory/latency and model-license proof.

### ONNX Runtime GenAI — LOWER-LEVEL FALLBACK / PROOF CANDIDATE
- Role: direct generative ONNX inference when lower-level control offers a measured benefit over Foundry Local.
- Caution: generative API remains evolving; keep adapter isolated and optional.
- Adoption gate: exact API/version stability, Windows execution provider, cancellation/resource behavior and packaged proof.

### ONNX Runtime — SPECIALIST REUSE
- Role: compact classifiers, rankers, embeddings, vision/audio and other task-specific models.
- Non-goal: installing ONNX Runtime is not itself a general reasoning brain.

## C. External local model service

### Ollama — INTEGRATED
Keep the existing direct local adapter for user-managed local models and model sharing outside Nika. This is independent of Embedded Brain; users may choose either.

### Generic OpenAI-compatible local endpoint — ADAPT
Useful for LM Studio/other compatible local services without changing agent/runtime contracts.

## D. Cloud/API intelligence

### Direct OpenAI-compatible/provider adapters — INTEGRATED FOUNDATION
Keep privacy/cost/error normalization behind ModelGateway.

### LiteLLM — OPTIONAL ADAPT
Use when broad provider normalization measurably reduces glue and its adopted package/license surface remains compatible. Do not make it the owner of Nika routing policy.

## E. Model files and distribution

- No large model is bundled into the mandatory Nika Core ZIP merely because its engine is supported.
- Model artifacts have identity, source, license, checksum, size, task/capability declaration and resource requirements.
- Program updates must not force re-downloading unchanged models.
- Model Engineering Lab benchmarks candidates before defaults/promotions change.

## F. Acceptance status vocabulary

Architecture-selected != dependency-declared != adapter-implemented != contract-tested != physical-hardware-proven != packaged-user-journey-proven.

Foundry Local can be integrated at the adapter/contract level before a real user-machine model proof, but status reports must state that difference explicitly.
