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

### Microsoft Foundry Local 1.2.x — PRIMARY ADAPT candidate/integration
- Official repository: `microsoft/Foundry-Local`.
- SDK license: MIT; each downloadable model has its own license and must be audited separately.
- Windows package policy: `foundry-local-sdk-winml>=1.2.3,<2`.
- Cross-platform package policy where needed: `foundry-local-sdk>=1.2.3,<2`.
- Exact SDK truth is evidence, not a stale prose pin: every physical/release proof records the actually installed package/version. The 2026-08-19 AUTO02 Windows resolver installed `foundry-local-sdk-winml==1.2.4`; Nika therefore does not describe 1.2.3 as the immutable current patch level.
- The two SDK package variants are mutually exclusive in one Python environment.
- Use the official Python SDK directly for embedded/in-process inference; the optional local web service is not required for normal Nika embedding.
- Nika adapter: `FoundryLocalProvider` behind `ModelProvider` / ModelGateway.
- Privacy: provider is LOCAL and can keep private/sensitive input on-device under Nika routing policy.
- Model management: no silent model download. Models are optional components outside the base EXE and require explicit install/download intent, model identity, separately reviewed license, checksum/resource evidence where required.
- Artifact identity: the public Python SDK's exact selected model/variant `id` is the provider artifact identity Nika can safely pin. Physical/release proof requires that exact ID in addition to the human-reviewed license reference. Nika does not depend on private SDK internals to invent model-license/version truth.
- Acquisition timeout/cancellation: explicit model download has its own total timeout. Timeout or caller cancellation signals the SDK's documented download cancellation event. The shared provider slot stays occupied until the native worker actually exits.
- Resource policy: Microsoft positions Foundry Local as single-user on-device inference rather than a concurrent server stack, so Nika serializes in-process completions per provider instance instead of pretending it has server-style batching/queueing. `ModelRequest.timeout_seconds` bounds the caller-visible wait. Optional Nika `ModelResourcePolicy` preflights CPU, memory percentage and minimum available memory through the existing resource-observer port before native inference starts. Importing the embedded provider must not require the concrete `psutil` observer; that adapter remains an optional runtime component.
- Lifecycle ownership: Foundry's manager is process-wide. A Nika provider instance tracks only models it loaded and unloads only those; it does not unload a model that was already loaded by another consumer. `close()` fails closed while timed-out/cancelled native work still owns the provider slot.
- Fallback safety: provider capabilities explicitly record whether hard cancellation is proven. ModelGateway does not launch a fallback after a timeout from a provider such as current Foundry Local whose active native inference cannot be proven stopped. Retryable non-timeout failures may still use an explicitly requested, privacy-prevalidated fallback route.
- Hardware: Windows WinML package is preferred for actual Windows hardware; final acceptance requires a physical-Windows inference proof.
- Physical proof path: `scripts/prove_foundry_local.py` requires Windows + the WinML package, exact alias/variant ID/license evidence, runs inference through the real ModelGateway, records platform/resource evidence without storing raw prompt/response text, performs provider-owned unload then reload/inference, and can optionally hash the cache tree. This script is preparation/evidence infrastructure until it is actually run on physical target hardware with a real model.
- Cancellation truth: current official Python docs explicitly expose cancellation for model/EP downloads. They do not document a hard-cancel primitive for an active non-streaming inference. Nika returns typed timeout/cancellation semantics at its async boundary but must not claim the underlying native inference thread was hard-killed until a focused proof or process-isolation design closes that gap.

### llama.cpp — FALLBACK / PROOF CANDIDATE
- Role: alternative embedded generative backend where GGUF model availability, CPU/Vulkan portability, performance or packaging wins a measured Nika benchmark.
- Integration: maintained binding/native adapter behind ModelGateway; do not vendor the repository wholesale.
- Adoption gate: exact binding/version/license, Windows x64 CPU/AMD path, cancellation behavior, model lifecycle, package size, memory/latency and model-license proof.
- Current decision: do not add it merely to increase adapter count while Foundry's primary physical proof is still the unresolved acceptance boundary.

### ONNX Runtime GenAI — LOWER-LEVEL FALLBACK / PROOF CANDIDATE
- Role: direct generative ONNX inference when lower-level control offers a measured benefit over Foundry Local.
- Caution: generative API remains evolving; keep adapter isolated and optional.
- Adoption gate: exact API/version stability, Windows execution provider, cancellation/resource behavior and packaged proof.
- Current decision: no new dependency until a measured Foundry gap or concrete specialist requirement justifies the additional runtime/package surface.

### ONNX Runtime — SPECIALIST REUSE
- Role: compact classifiers, rankers, embeddings, vision/audio and other task-specific models.
- Non-goal: installing ONNX Runtime is not itself a general reasoning brain.

## C. External local model service

### Ollama — INTEGRATED
Keep the existing direct local adapter for user-managed local models and model sharing outside Nika. This is independent of Embedded Brain; users may choose either.

The dedicated adapter uses Ollama's native `/api/chat`, disables streaming for the shared bounded request contract, defaults thinking output off, normalizes native usage and does not claim hard server-side cancellation without proof. Focused AUTO02 CI may run a small live Ollama model through ModelGateway; that evidence does not award Foundry hardware credit.

### Generic OpenAI-compatible local endpoint — ADAPT
Useful for LM Studio/other compatible local services without changing agent/runtime contracts. Shared schema normalization fails closed when message content or usage metadata is malformed rather than coercing invalid provider values into plausible Nika results.

## D. Cloud/API intelligence

### Direct OpenAI-compatible/provider adapters — INTEGRATED FOUNDATION
Keep privacy/cost/error normalization behind ModelGateway. Fallback between providers is opt-in per request, ordered, privacy-prevalidated and deadline-bounded; there is no silent cloud escalation. Malformed response content/usage is a typed provider error, not silently stringified/coerced data.

### LiteLLM — OPTIONAL ADAPT
Use when broad provider normalization measurably reduces glue and its adopted package/license surface remains compatible. Do not make it the owner of Nika routing policy.

## E. Model files and distribution

- No large model is bundled into the mandatory Nika Core ZIP merely because its engine is supported.
- Model artifacts have identity, source, license, checksum, size, task/capability declaration and resource requirements.
- For current Foundry Python integration, the exact public model/variant ID + exact installed SDK package/version + reviewed model-license reference + optional cache-tree checksum/bytes are the reproducible evidence surfaces available without private-SDK coupling.
- Program updates must not force re-downloading unchanged models.
- Model Engineering Lab benchmarks candidates before defaults/promotions change.

## F. Acceptance status vocabulary

Architecture-selected != dependency-declared != adapter-implemented != contract-tested != physical-hardware-proven != packaged-user-journey-proven.

Foundry Local can be integrated at the adapter/contract level before a real user-machine model proof, but status reports must state that difference explicitly. CI import tests, fake SDK managers, proof-script `--help`, or a real Ollama provider run are not physical Foundry model inference evidence.
