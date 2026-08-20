# Acceptance gates

A progress percentage moves only when evidence closes gates. Historical Core percentages and expanded Full Product Vision readiness are tracked separately; do not treat old Core credit as proof that every end-state workspace, ProductProject or user journey exists.

The 2026-08-20 Autonomous Product Factory expansion adds product-level gates. Backend coding-worker success is not evidence that Nika can autonomously create a complete product. Binding extended gates are in `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md`.

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

## ProductProject durability gate
A full digital-product goal that is expected to span multiple tasks/worker cycles must create a first-class durable ProductProject rather than being represented as only one AgentTask/CodingJob. Goal, research/decisions, requirements, architecture, repositories/components, milestones, team ownership, blockers, artifacts, releases/deployments and maintenance state must survive restart. Scope mutation is versioned/recorded rather than silently overwritten.

## Research-to-Product gate
When product discovery is requested, Universal Research must be able to produce a versioned evidence package that becomes ProductProject input without manual copy/paste. Product options and requirements remain traceable to evidence/decisions. If the user must choose a direction, implementation cannot silently choose on the user's behalf outside an authorized policy.

## Dynamic Team Composer gate
Team composition must be derived from ProductProject scope/risk/dependencies rather than a fixed hard-coded agent count. New specializations may be added during work without corrupting existing ownership. Child/worker permissions may not exceed the ProductProject ceiling.

## Multi-repository Product Factory gate
A ProductProject must be able to own one or multiple repositories/components with explicit ownership, dependencies, build/test commands and release identity. Parallel workers cannot silently edit overlapping ownership. Shared-contract changes require an explicit compatibility decision.

## Coding-worker / autonomous implementation gate
From structured ProductProject acceptance criteria, implementation must occur in isolated branches/workspaces; exact commits/diffs and machine-readable tests return to Nika; independent QA/audit may reject and force repair; workers cannot write directly to production main, self-promote failed candidates or widen their own permissions.

Coding-worker success alone is not Product Factory completion. The full representative proof is defined in `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md`.

## Multi-platform execution gate
Product Factory must be able to model execution/build nodes by platform/capability/resource. A task requiring unavailable Windows/Linux/macOS/GPU capability must fail clearly or route to an authorized suitable node; it must never fabricate successful evidence. Nodes receive only project-scoped paths/network/credentials.

## Credential/Identity Broker gate
ProductProject/workspace/task records, model prompts, Git and normal logs may not contain raw persistent passwords/API keys merely because an agent needs a connector. Persistent secret material is referenced through protected credential storage; workers receive least-privilege scoped/short-lived credentials where supported. Revocation blocks later use. A worker cannot enumerate unrelated credentials. Audit evidence records credential use without serializing the secret.

## Deployment gate
For a deployable test product, prove exact-SHA build/package, approved staging deployment, environment identity without secret leakage, post-deploy health checks and failure/rollback behavior. Production promotion remains inside the applicable user/policy authorization level. Source code alone is not a completed deployed product.

## Product operations/maintenance gate
A released test ProductProject must be able to create maintenance work from approved incident/health/dependency/security evidence, implement a fix in isolation, regression-test it and issue a versioned release. Production source is never silently self-modified outside ordinary implementation/review/release gates.

## Business Factory gate
Using a controlled sandbox/test channel, prove the lifecycle from business objective to research/opportunity, allowed lead/work-order creation, ProductProject handoff, Product Factory delivery and durable support/payment state. External communication/account/contract/financial actions must follow platform rules and Nika authorization. No spam, deceptive impersonation, prohibited automation or self-expansion of account/financial authority receives acceptance credit.

## IP/license/compliance gate
Competitor research may use permitted public information and standards to design an independent implementation; Product Factory must not treat access to proprietary source/assets/credentials as authorization to copy them. Adopted dependencies/tools record source/version/license/distribution obligations, and missing/unacceptable provenance blocks release.

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

## Autonomous Product Factory representative gate
From a clean packaged Windows Nika installation, issue a natural-language request such as:

`Create an accessible Windows personal-expense application. Research alternatives first, propose the architecture, wait for the required product decision, create/connect the repository, implement it, test it and prepare the Windows release.`

Without manual source-code copy/paste, the evidence chain must cover research/decision, durable ProductProject, requirements, team composition, repository creation/connection, isolated implementation, independent QA/accessibility review, package/release provenance, restart/resume and explicit remaining human-only acceptance items. A later deployment scenario additionally proves approved staging, health and rollback. Full detailed gates are in `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md`.

## Release gate
Fresh exact integration SHA -> cheap CI -> Windows package -> packaged smoke/E2E -> manifest/checksums/license/security scan -> user candidate. A previous, superseded or human-rejected artifact is never reissued as fresh. Any integrated product behavior change invalidates older candidate evidence until a fresh combined candidate passes the complete gate.
