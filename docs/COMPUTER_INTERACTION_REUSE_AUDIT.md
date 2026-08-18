# Computer Interaction reuse audit — eyes and hands

Updated: 2026-08-18.
Status: architecture/reuse decision only. No integration or milestone credit is claimed while M1/M2 executable CI is blocked.

## Goal
Nika Agent Lab must give a controlled digital worker structured perception and action across browsers, Windows applications, files and approved tools without building a fragile mouse-only automation stack from scratch.

Canonical priority:
1. structured application/API access;
2. accessibility/semantic trees;
3. deterministic UI automation;
4. screenshot/OCR/vision grounding only as fallback;
5. raw coordinate mouse/keyboard only as last resort.

All external actions remain behind Nika permissions, audit, cancellation, idempotency and approval policy.

## Current upstream audit
Fresh official upstream documentation was checked on 2026-08-18.

### Microsoft UFO² — ADAPT for Windows computer use
Source: https://github.com/microsoft/UFO/blob/main/documents/docs/ufo2/overview.md
License: MIT — https://github.com/microsoft/UFO/blob/main/LICENSE

Why it is valuable:
- Windows-first AgentOS rather than generic screenshot-only automation;
- combines Windows UI Automation with application-specific introspection and native APIs;
- hierarchical HostAgent/AppAgent model already solves cross-application delegation;
- provides UI inspection, screenshots, control targeting and actions;
- uses MCP action servers and hybrid GUI/API execution;
- documentation recommends native/structured execution where available and GUI fallback where needed.

Nika decision:
- do **not** reimplement a full Windows AgentOS before proving UFO² interoperability;
- keep Nika's future `WindowsInteractionPort` framework-neutral;
- first implementation candidate is a thin UFO²/MCP adapter or selective reuse of its action servers;
- Nika retains task ownership, permissions, approvals, audit, accessibility explanation and recovery policy;
- UFO² must never receive unrestricted shell/file/high-impact authority merely because its upstream agent can expose such actions.

Acceptance proof before adoption:
1. Windows 11 target-machine startup without administrator rights for ordinary operations;
2. enumerate UIA controls and accessible names from a real application;
3. focus and activate a named control deterministically;
4. type into a named edit field;
5. cross-application switch without coordinate assumptions;
6. cancellation during an active action;
7. all actions surfaced through Nika audit/permission boundaries;
8. NVDA remains usable while automation runs;
9. visual fallback is clearly marked as inferred rather than semantic evidence.

### Playwright — REUSE as the deterministic browser control baseline
Source: https://playwright.dev/python/docs/locators

Why it is valuable:
- locators provide auto-waiting and retry behavior;
- official guidance recommends role, label and other user-visible locators;
- role locators reflect how assistive technologies perceive controls;
- strict locator semantics fail when a target is ambiguous instead of silently clicking an arbitrary matching control.

Nika decision:
- Playwright is the default low-level browser automation adapter candidate;
- prefer `get_by_role`, labels, text and accessible names over CSS/XPath tied to implementation details;
- Nika browser actions must return evidence about the resolved semantic target;
- screenshots/vision supplement Playwright, not replace it.

Acceptance proof before adoption:
1. semantic reading of headings/forms/tables;
2. role/name targeting of controls;
3. strict failure on ambiguous targets;
4. bounded navigation/action timeout;
5. download/upload isolation to approved workspace paths;
6. session/auth handling without copying user browser profiles into the repository;
7. audit record for every state-changing browser action.

### Browser Use — ADAPT only after Playwright baseline proof
Source: https://github.com/browser-use/browser-use
License: MIT — https://github.com/browser-use/browser-use/blob/main/LICENSE

Why it is valuable:
- ready-made agent-oriented browser layer;
- model-agnostic provider options including local-model paths;
- MCP support and browser-specific agent abstractions.

Risk/constraint:
- the current package brings a large provider/tool dependency surface;
- its agent layer must not become a mandatory dependency of Nika Core;
- authentication examples may use persistent browser profiles, but Nika security policy forbids shipping or committing cookies/profiles/secrets.

Nika decision:
- keep as an optional higher-level `BrowserAgentAdapter` behind the deterministic Playwright port;
- do not vendor the repository;
- do not make Browser Use responsible for Nika permissions, long-term memory or product task state.

### pywinauto / direct Windows UIA — FALLBACK REUSE candidate
If UFO² proves too heavy, unstable or hard to isolate, use a smaller Windows UI Automation adapter rather than building low-level accessibility/Win32 discovery from scratch. This remains a fallback selection gate, not a committed dependency in this cycle.

## Accessibility Repair Agent mapping
For an interface NVDA cannot expose:
1. inspect DOM for web or UIA/accessibility tree for Windows;
2. return a structured text description with source/evidence confidence;
3. if structured semantics are insufficient, capture screenshot and invoke optional OCR/vision parser;
4. propose the safest available action target;
5. require Nika policy/approval for state-changing or high-impact actions;
6. if repeated use is needed, generate a narrow versioned helper/adapter in sandbox rather than recording raw coordinates as the permanent solution;
7. test the helper and store provenance, compatibility range and rollback information.

## Architecture boundary
Future domain contracts should be capability-oriented rather than framework-oriented, for example:
- `PerceptionPort`: enumerate windows/pages, semantic tree, text, control metadata, screenshot fallback;
- `BrowserInteractionPort`: navigate, resolve semantic target, read, fill, activate, upload/download;
- `WindowsInteractionPort`: enumerate/focus/read/invoke/set-value/launch/switch-window;
- `VisionFallbackPort`: screenshot -> grounded candidate controls/evidence;
- `InteractionActionResult`: requested target, resolved target, method used, semantic evidence, changed state, audit correlation ID.

Do not expose UFO², Playwright, Browser Use or UIA-specific classes in Nika Agent Lab domain APIs.

## Explicit non-goals
- no home-grown screen-coordinate agent as the primary implementation;
- no bypass of CAPTCHAs, anti-bot/security controls or platform restrictions;
- no hidden remote desktop control outside user permissions;
- no silent destructive/publishing/financial actions;
- no vendoring full upstream repositories when package/MCP/adapter integration is sufficient.

## Adoption order
1. Playwright proof for browser semantic actions.
2. UFO² Windows proof with UIA-first behavior and Nika audit/permission wrapper.
3. Browser Use only if its higher-level agent behavior measurably reduces Nika glue code beyond the Playwright baseline.
4. Vision/OCR grounding only for interfaces that cannot be represented semantically.

This audit intentionally prevents premature implementation while M1/M2 integration is externally blocked.