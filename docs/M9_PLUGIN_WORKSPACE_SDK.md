# M9 Plugin SDK and real workspace foundation

Status: SDK/workspace foundation GREEN / INTEGRATED via PR #21; M9 milestone credit remains withheld until the real semantic adapter follow-up also passes its exact gate and integrates.

## Scope

M9 turns the M1 minimum workspace entry-point contract into a downstream SDK and proves real independent workspace boundaries without changing the integrated M1-M8 product contracts.

Integrated foundation:

- `nika_core.plugins`: versioned `PluginManifest`, explicit API compatibility range, duplicate-capability rejection, capability risk declarations, lazy `importlib.metadata` discovery and explicit activation/deactivation through `PluginRuntime`;
- static `PluginRegistration` entry-point payloads so selecting a plugin registration loads package metadata/registration but does not construct the adapter until explicit activation;
- fail-closed runtime manifest identity checks plus cleanup of rejected adapter instances;
- `nika_core.workspaces`: versioned `WorkspaceManifest`, required plugin/API/capability declarations, workspace risk ceilings and traversal-safe path resolution;
- Software Factory workspace boundary with framework-neutral `CodingWorkerPort`, isolated repository-relative allowed paths, explicit test commands, network disabled by default and `CapabilityGap` evidence for missing-tool escalation;
- `SoftwareFactoryService` validation of returned worker evidence so changed paths cannot escape the declared allowed scope, required verification cannot disappear, and source changes require patch/commit evidence;
- Accessibility Repair workspace with browser DOM and Windows UI Automation semantic inspection contracts;
- `AccessibilityRepairService` that executes semantic DOM/UIA inspection first and consults vision/coordinate fallback only if semantic evidence exposes no usable controls;
- deterministic compatibility/isolation/security tests for API mismatch, missing capabilities, risk escalation, undeclared plugin grants, path traversal, coding-worker request/result isolation, lazy registration, rejected-adapter cleanup and fallback provenance/order.

Real-adapter acceptance follow-up:

- optional `browser` component uses maintained Playwright rather than a custom browser engine;
- `PlaywrightSemanticAdapter` reads the live Chromium accessibility tree through `Locator.aria_snapshot()` and returns Nika-owned DOM evidence;
- optional `windows-interaction` component uses pywinauto's UIA backend for a deterministic low-level Windows accessibility-tree adapter;
- `PywinautoUIAAdapter` is scoped to a target process and returns Nika-owned UIA evidence instead of leaking framework objects into the domain;
- focused CI jobs run a live headless Chromium semantic proof on Ubuntu and a live Windows UIA proof against a launched GUI process on Windows.

## REUSE / ADAPT / CUSTOM

### REUSE — Python packaging entry points

Python 3.12 `importlib.metadata.entry_points()` is the discovery mechanism. Discovery returns package metadata without loading selected plugin modules. When the user/system explicitly selects an entry point, the loaded payload must be a `PluginRegistration` containing a static manifest plus a lazy factory. Adapter construction is deferred until `PluginRuntime.activate()`.

The existing M1 `nika_core.workspaces` entry-point contract remains untouched. M9 adds a separate `nika_core.plugins` group for capability providers and does not replace the M1 registry.

### ADAPT — OpenHands for Software Factory

OpenHands Software Agent SDK/Agent Server remains the first coding-worker adapter candidate. Current official documentation exposes Python and REST APIs, predefined software-development tools, and local/remote agent execution. Nika does not import OpenHands types into domain contracts.

The M9 boundary is `CodingWorkerPort`. Any OpenHands adapter must receive a Nika `CodingRequest`, operate only in an isolated worktree/workspace, preserve cancellation and return changed-path plus patch/commit/test evidence. `SoftwareFactoryService` independently rejects out-of-scope results, so a worker cannot self-attest its way around Nika isolation.

No OpenHands runtime dependency is added to the base package in M9. A live model-backed OpenHands worker requires an explicitly configured model/provider and sandbox and therefore is not fabricated as a credential-free CI proof. The stable Nika port and output safety boundary are integrated now; the heavy worker remains an optional adapter/component.

### REUSE — Playwright browser semantics

Playwright is the browser semantic baseline. Current official documentation recommends user-facing role/label locators and exposes ARIA snapshots as a YAML representation of the accessibility tree. `PlaywrightSemanticAdapter` uses that maintained accessibility-tree API directly; Nika does not implement DOM accessibility computation itself.

### ADAPT — Windows UI Automation

Windows interaction remains UIA-first. The deterministic low-level adapter uses pywinauto's documented UIA backend and descendant wrappers behind `AccessibilityInteractionPort`. UFO² remains the heavier higher-level Windows worker candidate behind the same Nika boundary; it is not required merely to read a semantic tree. Mouse/coordinates remain fallback rather than the primary interface.

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
- translation of upstream semantic evidence into stable Nika evidence contracts;
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
7. A maintained Playwright adapter extracts real Chromium ARIA/accessibility evidence through the Nika interface.
8. A maintained Windows UIA adapter extracts real control evidence from a launched Windows process through the Nika interface.
9. No secret/session/cookie/browser-profile material is introduced.
10. Exact green follow-up head is merged to `main` before M9's 8% weight is credited.

PACKAGED, HUMAN_TESTED and NVDA_VERIFIED remain false for M9.
