# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT / PARALLEL-FIRST
Repository visibility observed this cycle: PUBLIC.

## Weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 100% of its 6% weight.
- M1 kernel foundation: GREEN / INTEGRATED, 100% of its 10% weight.
- M2 durable agent runtime: GREEN / INTEGRATED, 100% of its 11% weight.
- M3 memory/scheduler/resource control: GREEN / INTEGRATED, 100% of its 9% weight.
- M4 model gateway/tools/MCP: GREEN / INTEGRATED, 100% of its 8% weight.
- M5 accessible web-style Windows GUI: GREEN / INTEGRATED, 100% of its 11% weight.
- Overall proven final A–Z product progress is **55.0%**.

## Proven milestone evidence
- M1 exact green head `67df93c355e813dfc297bd1111df40d3c4ad6175`; Core CI run 74 success; merged as `b40ee58ce9c585efe7dad8ebfa23490e842c753a`.
- M2 exact green head `c890a5eadbea01afe92617f440ca83005c3b5f0c`; Core CI run 85 success; merged as `7c13b070d7b3c99c41e8cafaea855c9214322abe`.
- M3 exact green head `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI run 98 passed Ubuntu and Windows; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.
- M4 exact green head `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI run 112 passed Ubuntu, Windows, and focused live Ollama provider proof; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.
- M5 exact green head `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI run 137 passed shared Ubuntu verification, shared Windows verification, and the packaged PyInstaller/WebView2 UI Automation + keyboard/focus proof; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.

## M5 integrated evidence
- local semantic HTML/CSS/JavaScript application surface hosted through pywebview with the Windows renderer fixed to EdgeChromium/WebView2;
- supported pywebview local-path hosting rather than a discouraged `file://` application URL;
- narrow validated JavaScript-to-Python UI bridge; no arbitrary filesystem, shell or backend object exposure;
- centralized Action Registry/Keymap path for application shortcuts, including persisted remap/clear/restore/export/import/conflict behavior;
- Tasks, Agents, Workspaces, Logs, command and keyboard surfaces with semantic landmarks/headings/labels/table/status semantics;
- deterministic focus targets and live textual status rather than mouse-only feedback;
- bridge startup is fail-closed until asynchronous Action Registry/keymap loading completes; `window.pywebviewready` is used as documented with idempotent already-ready recovery;
- diagnostic PyInstaller one-dir Windows candidate starts successfully in CI;
- real packaged WebView2 descendants are discoverable in Windows UI Automation;
- packaged keyboard/focus proof verified `Alt+1` -> `Завдання` and `Ctrl+Shift+P` -> command input through the Action Registry/Keymap.

## M5 reuse gate
- ADAPT pywebview 6.2.1 behind the Nika shell boundary.
- REUSE EdgeChromium/WebView2 as the Windows renderer/accessibility host.
- DEFER/REUSE when complexity warrants it: React + TypeScript + Vite; the current small shell remains native semantic HTML/JS rather than adding a framework without measured benefit.
- REUSE selectively later: React Aria Components for composite widgets where they materially improve keyboard/focus/name semantics.
- CUSTOM (thin): Nika bridge validation, Action Registry/Keymap integration, accessible status/log behavior and deterministic focus restoration.
- Draft PR #11 was inspected and its UI candidate was not reused as-is because it hard-coded an Escape application action outside the canonical Keymap path.

## Defects found and repaired during M5 acceptance
- Core CI run 129 proved WebView2 descendants were visible but found a real focus failure: `Alt+1` left focus on `Створити завдання` rather than `Завдання`.
- The first readiness repair exposed a deeper defect in run 133: the explicit ready status never appeared.
- Upstream review identified two incorrect host assumptions: the `pywebviewready` listener was attached to `document` instead of `window`, and the shell forced a discouraged `file://` URL.
- The accepted repair uses the documented window event, idempotent already-ready initialization, awaited keymap loading and pywebview-supported local-path hosting. Exact-head run 137 then passed the real packaged gate.
- Superseded/cancelled runs were not credited.

## Governance consistency
Repository metadata is PUBLIC despite user wording referring to a private repository. PR #4 remains OPEN/non-mergeable and is not counted as integrated evidence. `AGENTS.md` currently references `docs/PARALLEL_DEVELOPMENT_POLICY.md`, but that file is absent from main and exists only in the stale PR #4 proposal; this governance inconsistency remains unintegrated and must not be treated as accepted policy text beyond the parallel-first rules already present in integrated canonical documents. PR #11 remains a separate draft downstream M5–M12 foundation lane and receives no product-weight credit unless each reusable slice is independently reviewed, proven and integrated.

## Truth state
- M0: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M1: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M2: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M3: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M4: IMPLEMENTED / GREEN / INTEGRATED; not PACKAGED; not HUMAN_TESTED.
- M5: IMPLEMENTED / GREEN / INTEGRATED; diagnostic packaged acceptance proof exists, but this is not an M11 release package; not HUMAN_TESTED; not NVDA_VERIFIED.
- M6–M12: PREPARED/parallel work only unless separately evidenced; draft PR #11 does not receive milestone credit.
- No standalone release Windows package yet.
- No human NVDA verification yet; automation must never award NVDA_VERIFIED.

## Current weighted milestone
M6 — Agent Builder and permissions.

## Next LARGE coherent batch
Audit the M6 portion of draft PR #11 against current M4 model/tool contracts and the integrated runtime/permission boundaries. Build the largest safe Agent Builder slice: versioned Pydantic agent specification, deterministic validation/compilation, natural-language draft adapter behind Model Gateway structured output, explicit R0–R4 tool/permission review, fail-closed unknown tool/permission handling, schedule/model/budget references, activation/versioning persistence, audit and regression tests. Dangerous capabilities must never activate silently. Credit M6 only after its exact Ubuntu + Windows acceptance evidence is green and the candidate is integrated. Keep HUMAN_TESTED/NVDA truth separate.
