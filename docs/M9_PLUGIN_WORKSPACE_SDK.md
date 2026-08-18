# M9 Plugin SDK and real workspace foundation

Status: IMPLEMENTED candidate on `dev-b/m9-plugin-workspace-sdk`; no milestone credit before exact Ubuntu + Windows green CI and integration.

## Scope

This M9 slice turns the M1 minimum workspace entry-point contract into a downstream SDK without changing M1-M4 implementation.

Implemented:

- `nika_core.plugins`: versioned `PluginManifest`, explicit API compatibility range, duplicate-capability rejection, capability risk declarations, lazy `importlib.metadata` entry-point discovery and explicit activation/deactivation through `PluginRuntime`;
- `nika_core.workspaces`: versioned `WorkspaceManifest`, required plugin/API/capability declarations, workspace risk ceilings and traversal-safe path resolution;
- `Software Factory` proof workspace with a framework-neutral `CodingWorkerPort`, isolated repository-relative allowed paths, explicit test commands, network disabled by default and `CapabilityGap` evidence for missing-tool escalation;
- `Accessibility Repair` proof workspace with browser DOM and Windows UI Automation semantic inspection contracts; vision/coordinate evidence is explicitly fallback evidence and cannot claim perfect semantic confidence;
- deterministic compatibility tests for API mismatch, missing capabilities, risk escalation, undeclared plugin grants, path traversal, coding-worker isolation and fallback-evidence truthfulness.

## REUSE / ADAPT / CUSTOM

### REUSE — Python packaging entry points

Python 3.12 `importlib.metadata.entry_points()` is the discovery mechanism. Discovery returns package metadata without eagerly loading arbitrary plugin code. Nika loads only an explicitly selected entry point at the activation boundary.

The existing M1 `nika_core.workspaces` entry-point contract remains untouched. M9 adds a separate `nika_core.plugins` group for capability providers and does not replace the M1 registry.

### ADAPT — OpenHands for Software Factory

OpenHands Software Agent SDK/Agent Server remains the first coding-worker adapter candidate because its supported SDK exposes code-oriented agents, tools and isolated workspace/server execution. Nika does not import OpenHands types into domain contracts. A future adapter must implement `CodingWorkerPort`, obey repository-relative allowed paths, preserve cancellation and return patch/commit/test evidence.

No OpenHands dependency is added in this baseline commit: adoption still requires a focused proof against the current supported package/API and security boundary.

### ADAPT — Playwright and Windows semantic interaction

Playwright remains the browser baseline because its role/label locators target user-visible accessibility semantics. Windows interaction remains UIA-first (with the previously audited UFO²/UIA direction). The `AccessibilityInteractionPort` deliberately exposes Nika evidence rather than Playwright/UFO/UIA objects.

Vision/OCR/coordinate interaction remains fallback only and must preserve lower-confidence provenance.

### CUSTOM (thin) — Nika compatibility and permission policy

Nika owns only the product-specific layer that upstream frameworks do not own:

- plugin/workspace identities and versioned manifests;
- capability declarations tied to the existing `ToolRisk` vocabulary;
- workspace-specific maximum risk ceilings;
- compatibility validation before activation;
- isolated workspace path boundaries;
- capability-gap evidence that can be handed to Software Factory when a safe capability is missing.

## Capability escalation direction

The intended later execution loop is:

`task -> existing semantic/API capability -> explicit failure evidence -> CapabilityGap -> reuse search -> CodingWorkerPort in isolated worktree -> tests/security evidence -> versioned plugin/adapter -> compatibility validation -> return to original task`.

This contract does not let a runtime agent silently install arbitrary code or widen permissions. Plugin activation and later publication/integration remain explicit Nika-controlled gates.

## Acceptance requirements before M9 credit

1. Complete `python scripts/verify.py` succeeds on the exact candidate on Ubuntu and Windows.
2. Plugin API mismatch and capability/risk escalation fail closed.
3. Workspace traversal outside the configured root fails closed.
4. Software Factory proof preserves allowed-path/network defaults and returns test/patch evidence through a replaceable port.
5. Accessibility Repair proof preserves DOM/UIA-before-vision ordering at the contract and documentation level.
6. No secret/session/cookie/browser-profile material is introduced.
7. Exact green head is merged to `main` before M9's 8% weight is credited.

PACKAGED, HUMAN_TESTED and NVDA_VERIFIED remain false for this M9 source slice.
