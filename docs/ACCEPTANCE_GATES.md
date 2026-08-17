# Acceptance gates

A progress percentage moves only when evidence closes gates.

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

## Model gateway gate
The same semantic scenario runs through mock and at least one real provider through the same Nika interface. Provider failure maps to a typed Nika error and respects timeout/cancellation.

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
Fresh exact integration SHA -> cheap CI -> Windows package -> packaged smoke/E2E -> manifest/checksums/license/security scan -> user candidate. A previous or human-rejected artifact is never reissued as fresh.
