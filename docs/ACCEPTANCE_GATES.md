# Acceptance gates

A progress percentage moves only when evidence closes gates. Historical Core percentages and expanded Full Product Vision readiness are tracked separately; do not treat old Core credit as proof that every end-state workspace or user journey exists.

## Universal gates
- source compiles and imports;
- deterministic unit tests for changed behavior;
- no secret/token/session/cookie files;
- migration/restart behavior tested when state changes;
- error path tested for critical operations;
- exact branch/SHA recorded;
- accessibility semantics tested for user-facing controls;
- documentation/status updated in the same coherent batch.

## Reuse gate
Before a new subsystem is implemented, the cycle records REUSE, ADAPT or CUSTOM and the maintained upstream choices inspected. CUSTOM without justification does not pass review.

## Durable runtime gate
Kill/restart a task after a completed step; resume from persisted state without repeating the completed side effect. Corrupt/invalid checkpoint must fail closed.

## Deterministic Brain gate
Prove a useful task can be planned and executed with **no language model configured at all**. The scenario must use explicit world state/goals/actions, invoke registered Nika tools, reach the declared goal, reject an impossible goal cleanly, and re-plan from changed state without repeating already-completed work. A high-impact tool selected by the planner must still be denied without the normal Nika approval evidence. Planner wall time and returned plan length must be bounded; an oversized plan is rejected before any tool action executes.

## Model gateway gate
The same semantic scenario runs through mock/no-model behavior and at least one real model provider through the same Nika interface. Provider failure maps to a typed Nika error and respects the provider's documented timeout/cancellation guarantees. Any provider whose underlying engine cannot yet prove hard inference cancellation must record that limitation rather than receiving false acceptance credit. Provider response normalization fails closed on malformed text/usage rather than manufacturing plausible values.

Fallback is **explicit, not implicit**. A request may nominate ordered fallback provider IDs, but the complete route must be validated before the first provider receives sensitive data. Fallback occurs only after a retryable typed failure/timeout, never after cancellation or a non-retryable authentication/policy/resource failure, and the original total request deadline remains authoritative across all attempts.

## Embedded Brain / Foundry Local gate
The Microsoft Foundry Local adapter must remain behind ModelGateway and leak no Foundry SDK types into Nika domain contracts. Automated tests must prove local/private routing, explicit model selection, fail-closed behavior when an uncached model would require an unapproved download, exact public variant-ID pinning when requested, bounded/serialized in-process resource use, resource-policy rejection before native model execution, typed timeout behavior, explicit-download timeout/cancellation slot retention, provider-owned unload semantics and normal response/error normalization. Windows CI must prove the selected official Foundry Local SDK package resolves/imports on every active AUTO02 successor lane, without downloading a large model merely for CI.

Full production credit additionally requires a focused **real physical-Windows hardware inference proof** with the exact adopted SDK package/version, exact public model/variant identity, human-reviewed model-license evidence, model/cache checksum evidence when release identity requires it, hardware/platform and resource evidence, successful inference through the real Nika `ModelGateway`, and clean provider-owned unload/reload inference behavior. `scripts/prove_foundry_local.py` is the controlled evidence collector: it requires exact `--model-id`, must not download a model unless `--allow-download` is explicitly supplied, and the operator must supply `--model-license` rather than letting code invent a model license. Raw prompt/response text is not required in the evidence artifact; response presence/length/hash and normalized usage/latency are sufficient plumbing evidence. A cloud call, local HTTP mock, live Ollama call or SDK-import-only job is not a substitute for that Foundry hardware proof.

If the selected model is already loaded by another Foundry consumer, the physical lifecycle proof must not unload it or claim ownership. The controlled proof fails rather than disrupting another consumer. Likewise, a timed-out native inference/download that still owns the provider slot prevents shutdown/unload until that worker exits.

Alternative embedded engines such as llama.cpp or ONNX Runtime GenAI receive no acceptance credit merely by being named in architecture docs; each requires its own measured adapter proof before activation. Adding one is not required while Foundry's unresolved gate is simply physical target-hardware evidence rather than an identified engine limitation.

## Capability Escalation / Toolsmith gate
Start a durable task that lacks a required capability. The task must record the capability gap, search existing registered/upstream capability options first, route a bounded request to the Software Factory/CodingWorker only when necessary, test the candidate in isolation, register/version it under normal permission rules, and resume the original task from checkpoint. A failed capability build must leave the original task safely blocked with evidence. The Toolsmith loop may not write directly to production main or widen its own permissions.

## Product Journey gate
A user-facing subsystem is not complete until the same real capability is proven through the final product path:

`packaged Windows UI -> semantic action/command -> validated bridge/API -> real Nika service/runtime -> persisted state/result -> accessible visible feedback -> restart/resume where relevant`.

Placeholders, dead buttons, mock-only lists, or backend-only tests do not close this gate. The packaged path must cover success and critical error behavior, keyboard/focus semantics, and WebView2/UIA discovery where applicable. Final human NVDA verification remains separate and human-only.

## Action Registry / keymap gate
Every app action has a stable ID. User overrides persist outside source. Remap/clear/restore works; duplicate bindings are detected; standard edit keys remain usable in editable controls; all critical actions are also reachable from semantic UI controls.

## Agent Builder gate
Natural-language draft must become a validated versioned config. Unknown tool/permission values fail closed. Dangerous tools cannot be activated silently.

## Multi-agent gate
Bounded parallel agents operate with typed messages and quotas. Failure/cancel in one worker does not corrupt team state.

## Learning gate
A challenger is evaluated against explicit metrics and fixed/held-out data. Promotion/rollback is reproducible and logged. No metric means no autonomous promotion.

## Windows/WebView2/NVDA gate
Semantic HTML/unit accessibility checks -> packaged WebView2 host UI Automation discovery -> keyboard/focus flow -> packaged startup. The real descendant controls must be discoverable by UI Automation in the packaged app. Final NVDA VERIFIED remains human-only.

## Release gate
Fresh exact integration SHA -> cheap CI -> Windows package -> packaged smoke/E2E -> manifest/checksums/license/security scan -> user candidate. A previous, superseded or human-rejected artifact is never reissued as fresh. Any integrated product behavior change invalidates older candidate evidence until a fresh combined candidate passes the complete gate.
