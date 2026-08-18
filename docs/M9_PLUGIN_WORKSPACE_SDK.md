# M9 Plugin SDK and real workspace foundation

Status: IMPLEMENTED candidate on `dev-b/m9-plugin-workspace-sdk`; no milestone credit before exact Ubuntu + Windows green CI and integration.

## Scope

This M9 slice turns the M1 minimum workspace entry-point contract into a downstream SDK without changing the integrated M1-M8 product contracts.

Implemented:

- `nika_core.plugins`: versioned `PluginManifest`, explicit API compatibility range, duplicate-capability rejection, capability risk declarations, lazy `importlib.metadata` discovery and explicit activation/deactivation through `PluginRuntime`;
- static `PluginRegistration` entry-point payloads so selecting a plugin registration loads package metadata/registration but does not construct the adapter until explicit activation;
- fail-closed runtime manifest identity checks plus cleanup of rejected adapter instances;
- `nika_core.workspaces`: versioned `WorkspaceManifest`, required plugin/API/capability declarations, workspace risk ceilings and traversal-safe path resolution;
- Software Factory proof workspace with a framework-neutral `CodingWorkerPort`, isolated repository-relative allowed paths, explicit test commands, network disabled by default and `CapabilityGap` evidence for missing-tool escalation;
- `SoftwareFactoryService` validation of returned worker evidence so changed paths cannot escape the declared allowed scope, required verification cannot disappear, and source changes require patch/commit evidence;
- Accessibility Repair proof workspace with browser DOM and Windows UI Automation semantic inspection contracts;
- `AccessibilityRepairService` that executes semantic DOM/UIA inspection first and consults vision/coordinate fallback only if semantic evidence exposes no usable controls;
- deterministic compatibility/isolation/security tests for API mismatch, missing capabilities, risk escalation, undeclared plugin grants, path traversal, coding-worker request/result isolation, lazy registration, rejected-adapter cleanup and fallback provenance/order.

## REUSE / ADAPT / CUSTOM

### REUSE — Python packaging entry points

Python 3.12 `importlib.metadata.entry_points()` is the discovery mechanism. Discovery returns package metadata without loading selected plugin modules. When the user/system explicitly selects an entry point, the loaded payload must be a `PluginRegistration` containing a static manifest plus a lazy factory. Adapter construction is deferred until `PluginRuntime.activate()`.

The existing M1 `nika_core.workspaces` entry-point contract remains untouched. M9 adds a separate `nika_core.plugins` group for capability providers and does not replace the M1 registry.

### ADAPT — OpenHands for Software Factory

OpenHands Software Agent SDK/Agent Server remains the first coding-worker adapter candidate. Current official documentation exposes `openhands-sdk`, optional tools/workspace/agent-server packages, local and remote/sandboxed workspace modes and Python/REST APIs. Nika does not import OpenHands types into domain contracts.

The M9 boundary is `CodingWorkerPort`. Any OpenHands adapter must receive a Nika `CodingRequest`, operate only in an isolated worktree/workspace, preserve cancellation and return changed-path plus patch/commit/test evidence. `SoftwareFactoryService` independently rejects out-of-scope results, so a worker cannot self-attest its way around Nika isolation.

No OpenHands runtime dependency is added to the base package in this slice. A focused optional-component proof remains required before shipping that worker because model credentials, sandbox backend and package footprint are deployment choices rather than core requirements.

### ADAPT — Playwright and Windows semantic interaction

Playwright remains the browser baseline because current official documentation recommends user-facing role/label locators that align with accessibility semantics. Windows interaction remains UIA-first, with the audited UFO²/native-UIA direction as the heavier Windows adapter candidate. `AccessibilityInteractionPort` exposes only Nika evidence rather than Playwright/UFO/UIA implementation objects.

The concrete M9 workspace service enforces the ordering contract: semantic browser DOM or Windows UIA inspection occurs before optional visual fallback. Vision/OCR/coordinate evidence must retain lower-confidence provenance and cannot claim perfect semantic confidence.

### CUSTOM (thin) — Nika compatibility and permission policy

Nika owns only product-specific semantics that upstream frameworks cannot own:

- plugin/workspace identities and versioned manifests;
- capability declarations tied to the existing `ToolRisk` vocabulary;
- workspace-specific maximum risk ceilings;
- compatibility validation before activation;
- isolated workspace path boundaries;
- lazy adapter activation and rejected-instance cleanup;
- independent validation of coding-worker output scope/evidence;
- semantic-before-vision fallback policy;
- capability-gap evidence that can be handed to Software Factory when a safe capability is missing.

## Capability escalation direction

The intended execution loop is:

`task -> existing semantic/API capability -> explicit failure evidence -> CapabilityGap -> reuse search -> CodingWorkerPort in isolated worktree -> tests/security evidence -> versioned plugin/adapter -> compatibility validation -> return to original task`.

This contract does not let a runtime agent silently install arbitrary code, widen permissions or write production `main`. Plugin activation and later publication/integration remain explicit Nika-controlled gates.

## Acceptance requirements before M9 credit

1. Complete `python scripts/verify.py` succeeds on the exact candidate on Ubuntu and Windows.
2. Plugin API mismatch and capability/risk escalation fail closed.
3. Entry-point registration does not instantiate adapters before explicit activation; rejected runtime adapters are closed.
4. Workspace traversal outside the configured root fails closed.
5. Software Factory validates both requested allowed paths and returned changed paths/test/patch evidence.
6. Accessibility Repair proves semantic DOM/UIA inspection occurs before visual fallback and visual provenance cannot claim perfect semantic confidence.
7. No secret/session/cookie/browser-profile material is introduced.
8. Exact green head is merged to `main` before M9's 8% weight is credited.

PACKAGED, HUMAN_TESTED and NVDA_VERIFIED remain false for this M9 source slice.
