# M5 accessible Windows UI — reuse and acceptance record

Date: 2026-08-18
Branch: `dev/m5-accessible-ui`

## Decision

M5 remains an ADAPT/REUSE-first slice around a narrow Nika-owned accessibility contract.

- **ADAPT** pywebview 6.2.1 with explicit `edgechromium` renderer on Windows. The JS API remains a narrow validated Nika facade rather than exposing arbitrary backend objects.
- **REUSE** EdgeChromium/WebView2 as the renderer/accessibility host boundary.
- **REUSE/ADAPT later when complexity justifies it** React + TypeScript + Vite. Current M5 proof intentionally stays native semantic HTML/JavaScript because the shell is still small; introducing a frontend framework does not itself improve the packaged WebView2/UIA boundary and would add build/package surface before the host gate is proven.
- **REUSE selectively later** React Aria Components for composite widgets where they provide measurable keyboard/focus/name behavior beyond native semantic HTML. Native `button`, `textarea`, headings, landmarks, table semantics and labels remain preferred for the current shell.
- **CUSTOM (thin)** Nika Action Registry/Keymap integration, validated bridge commands/results, accessible live status/log surfaces and deterministic focus restoration because these are product-specific semantics.

## Fresh upstream check

Current pywebview documentation confirms that `js_api` methods are asynchronous Promise-returning calls and that `pywebviewready` is the supported DOM event for knowing that `window.pywebview.api` exists. Therefore visible DOM is not sufficient proof that Action Registry/keymap data has already completed its asynchronous bridge round trip.

Current Vite documentation continues to provide official React/TypeScript integration and production static bundling. React Aria continues to target WAI-ARIA-aligned keyboard and screen-reader behavior. Neither is adopted merely for popularity: the present native shell is simpler and already exposes semantic controls; React/Vite/React Aria remain the preferred scale-up path once M5 host accessibility is green and UI complexity warrants a component build system.

## PR #11 audit

Draft PR #11 is not a merge source for M5 as-is. Its UI candidate contains a hard-coded `Escape` application action and does not route all application shortcuts through the canonical Action Registry/Keymap. M5 therefore proceeds on the independent `dev/m5-accessible-ui` branch from current green main.

## Packaged gate defect and repair

Core CI run 129 proved that the packaged WebView2 accessibility descendants were discoverable, but the first keyboard/focus step failed: `Alt+1` left focus on `Створити завдання` instead of moving to the `Завдання` heading.

Root cause: the packaged DOM/UIA tree can become visible before the asynchronous `list_actions()` bridge call has finished. The page announced readiness before awaiting that call, so the hotkey test could run while the in-memory action list was still empty.

Repair:
- `pywebviewready` now awaits `refreshKeymap()` before setting explicit Nika-ready state;
- keyboard dispatch is disabled until Action Registry/keymap loading is complete;
- failure to load the bridge/keymap is announced fail-closed rather than silently pretending readiness;
- focus restoration uses a dedicated DOM helper and verifies the target element exists;
- packaged UIA proof waits for the explicit `Nika Core готова до роботи.` descendant before sending application hotkeys;
- regression tests lock the readiness-before-hotkey ordering.

## Acceptance truth

M5 receives no weighted credit until the exact candidate passes:
1. shared source/tests on Ubuntu;
2. shared source/tests on Windows;
3. packaged PyInstaller one-dir startup;
4. real WebView2 UI Automation descendant discovery;
5. keyboard/focus flow through remappable Action Registry/Keymap commands.

`HUMAN_TESTED` and `NVDA_VERIFIED` remain false until an actual human Windows/NVDA session is completed.
