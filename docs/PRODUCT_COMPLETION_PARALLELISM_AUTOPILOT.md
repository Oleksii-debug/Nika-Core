# Nika Core — product completion requirements: performance, parallel model execution, and Autopilot interoperability

Status: binding product requirement for the final Windows Nika product.

## 1. Whole-product rule

Nika Core is developed and accepted as one end-to-end product, not as independent feature islands. A subsystem is only complete when it composes with the current Windows product, durable state, model runtime, Product Factory, safety boundaries, release packaging, restart/recovery, and accessibility paths without regressing the whole product.

## 2. Performance is a product requirement

The final Windows product must remain responsive under realistic concurrent work. Performance is not a post-release optimization.

Required properties:

- UI/keyboard/NVDA interaction must not be blocked by long model, research, worker, browser, file, or network operations.
- Long operations must execute off the UI thread and expose bounded status/cancellation state.
- Startup and restart must avoid unnecessary re-inference, re-download, full rescans, or duplicate work when durable verified state is already available.
- Queueing/backpressure must be explicit and bounded; overload must degrade predictably rather than freeze the application.
- Work that is independent must be allowed to execute concurrently, subject to resource, permission, provider, rate-limit, and safety constraints.
- Expensive shared resources must use admission control so parallel work does not exhaust RAM/VRAM/CPU, disk, sockets, or provider quotas.
- Release acceptance must include representative concurrency/load checks and regression budgets for user-visible responsiveness, startup/restart, and parallel model dispatch.

## 3. Parallel local and API model execution is mandatory

Nika must support true concurrent model work. It must not impose one global FIFO model queue that serializes unrelated inference requests.

The product must be able to use, at the same time:

- multiple local models;
- multiple API/cloud models;
- a mix of local and API models;
- multiple requests to the same provider/model when provider limits and local resources permit;
- multiple agents/tasks that independently use different models in parallel.

### 3.1 Concurrency semantics

The canonical model runtime must provide asynchronous dispatch with per-request identity, cancellation, timeout, budget, result provenance, and durable completion state. Independent requests may run concurrently. Ordering constraints are scoped only to work that actually has a dependency, exclusivity requirement, rate limit, or shared-resource conflict.

There must be no architecture-wide mutex or single-worker bottleneck around ModelGateway/model execution.

### 3.2 Local-model resource scheduling

For local models, parallelism must be resource-aware rather than blindly unlimited. Nika must be able to admit several local models concurrently when RAM/VRAM/CPU capacity permits, and must reject, defer, or reduce concurrency before the machine becomes unresponsive.

The scheduler/admission policy must distinguish at least:

- model identity/runtime;
- expected RAM/VRAM footprint where available;
- CPU/GPU execution class;
- currently active local inference sessions;
- per-task priority/cancellation;
- user-configured concurrency/resource ceilings.

### 3.3 API-model resource scheduling

For API/cloud models, concurrency must be independently bounded per provider/account/model according to configured policy and observed rate limits. One slow provider must not block unrelated providers or local inference.

### 3.4 Fan-out/fan-in is first-class

Nika must support one task intentionally dispatching to several models in parallel, collecting all bounded results, and then applying an explicit aggregation/review step. This is required for research, adversarial review, comparison, ensemble/consensus, developer-auditor workflows, and Product Factory work.

A partial provider failure must be represented explicitly; it must not silently convert a multi-model run into a false full-success result.

## 4. Autopilot interoperability

The existing Autopilot remains a browser automation runtime for authenticated Chrome. Browser truth remains owned by the extension/browser side; Nika owns higher-level orchestration, product/task state, analysis, policies, and durable results.

The preferred product architecture is interoperability first, with later selective integration where it clearly improves the final product.

### 4.1 Required shared protocol

Nika and Autopilot must be able to exchange typed messages over a local authenticated bridge. The preferred transport is Chrome Native Messaging for the browser-extension boundary, with an optional authenticated localhost WebSocket/HTTP/JSON-RPC transport for broader local composition where justified.

Messages must carry, at minimum:

- job/task identity;
- target/browser-session identity;
- requested action;
- idempotency key;
- permission/authority context;
- timeout/cancellation identity;
- structured result/error;
- evidence/provenance reference without leaking secrets.

### 4.2 Nika -> Autopilot

Nika must be able to ask Autopilot to perform browser work such as navigating authenticated web applications, sending requests into pinned chats, waiting for results, collecting structured responses, and returning them to Nika for analysis.

Nika may orchestrate multiple Autopilot targets concurrently when browser/session/site constraints permit.

### 4.3 Autopilot -> Nika

Autopilot must be able to submit collected responses/events to Nika, ask Nika to analyze/aggregate them, obtain the next bounded instruction, and continue a workflow without copying state manually between applications.

### 4.4 Parallel multi-chat workflow

A representative acceptance journey must cover this product behavior:

1. Nika receives one user goal.
2. Nika selects several browser chat targets and/or direct model targets.
3. Independent targets are dispatched in parallel, not serially by default.
4. Autopilot sends the browser-chat requests through the user's authenticated browser sessions while Nika may simultaneously call local/API models directly.
5. Results stream back independently with exact source/target identity.
6. Nika aggregates, compares, audits, or routes follow-up work.
7. Restart/resume does not duplicate already-completed browser/model effects.
8. The user receives one coherent final result with provenance and partial-failure truth.

## 5. Integration decision

Do not collapse Autopilot into Nika merely to reduce the number of executables. Keep the browser extension as the browser-specific execution surface unless a later bounded component is demonstrably better hosted inside Nika.

The default target architecture is therefore:

Nika Core (orchestration, models, Product Factory, durable state, policy, analysis)
<-> authenticated local bridge
<-> Autopilot browser extension (browser DOM/session execution)
<-> authenticated browser applications.

This preserves separation of authority while still making the two products behave as one coordinated system to the user.

## 6. Acceptance consequences

Nika is not final merely because single-model inference works, because several models can be configured, or because Autopilot can operate independently.

Final acceptance requires proof that:

- unrelated local/API model requests genuinely overlap in time under allowed resources;
- no global model serialization bottleneck exists;
- fan-out/fan-in returns source-bound results and explicit partial failures;
- the Windows UI remains responsive while concurrent model/browser/worker work is active;
- cancellation and restart are safe under concurrency;
- Nika and Autopilot can exchange authenticated idempotent jobs/results without manual copy/paste;
- one representative multi-chat plus direct-model workflow completes end-to-end;
- all of the above are exercised from the packaged Windows product and do not bypass canonical safety/provenance/state authorities.
