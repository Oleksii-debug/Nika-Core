# DEV04 Windows UIA semantic vertical

Status: IMPLEMENTED on DEV04 branch; GREEN/INTEGRATED/PACKAGED/HUMAN_TESTED/NVDA_VERIFIED require separate evidence.

## Architecture decision

The Windows semantic substrate is Microsoft UI Automation. The first production-intended adapter is pywinauto with the `uia` backend behind Nika-owned records and contracts. No pywinauto/UIA object is allowed into `nika_core.interaction` domain types.

Top-level selection is strict and explicit: the caller supplies PID plus either exact window title or HWND. The adapter enumerates process-scoped top-level windows and requires exactly one match. It never uses `Application.top_window()`, pick-first behavior, `.first()`, `.nth()`, coordinates or bounds for target selection.

The stable application/window identity is executable path + PID + process creation time + HWND + adapter generation. A PID reuse, process restart or HWND replacement after binding raises `StaleSnapshotError`; it is not silently rebound.

Control identity is based on UIA RuntimeId under the already-bound HWND. A control without RuntimeId is rejected instead of receiving a positional identity. Bounds are copied only as evidence.

## Supported semantic evidence/actions

The pywinauto boundary exposes Nika-owned evidence for these UIA patterns when available: Invoke, Value, SelectionItem, Toggle, ExpandCollapse, ScrollItem, Text and Window. Current `InteractionAction` mappings directly exercise Invoke, Value, SelectionItem, Toggle and ExpandCollapse; focus is set and verified separately. ScrollItem/Text/Window are reported as capability evidence pending a Nika domain action that requires them rather than expanding the action enum speculatively.

The adapter implements the existing `InteractionAdapter` observe/focus/act/verify surface. It therefore remains under the integrated `SemanticInteractionCoordinator` safety sequence and its R0-R4 permission, approval and idempotency policy. Permission denial is not a vision/coordinate fallback.

## Physical Windows proof

`scripts/dev04_windows_uia_proof.py` launches a non-admin WinForms fixture from `scripts/fixtures/dev04_uia_winforms_fixture.ps1`. The proof requires exact process/window identity, resolves named Edit/Button/CheckBox controls semantically, captures/changes/restores focus, writes Ukrainian UTF-8 text through the Value pattern, toggles a checkbox, invokes a named button and verifies the resulting semantic state. It records that coordinates were not used and never writes HUMAN_TESTED or NVDA_VERIFIED.

Core CI is extended only for the DEV04 UIA branch (plus the existing historical M9 proof branch) to run the deterministic UIA tests and the live WinForms proof on `windows-latest`, after exact-candidate checkout identity validation.

## Deterministic defect-family coverage

`tests/test_interaction_windows_uia_adapter.py` covers zero/multiple window matches, explicit HWND selection, PID/start-time/HWND/generation binding, process restart, HWND replacement, missing RuntimeId, moved bounds, focus verification/restoration, pattern action mapping, hidden/disabled controls, stale control reuse, pattern evidence and the raw-UIA promotion policy.

## pywinauto vs raw IUIAutomation

Raw IUIAutomation/UIAutomationClient remains a measured comparison candidate, not a second runtime kernel. `UIABackendMeasurement` and `choose_measured_backend()` make the promotion rule executable: raw UIA must first match exact identity, strict ambiguity and focus-safety evidence, then show a material measured win (additional pattern coverage or at least 20% lower median observation latency). With no valid raw measurement, pywinauto remains selected.

This branch intentionally does not claim a raw-IUIAutomation win because no comparable physical measurement has yet been produced. Microsoft `winapp` remains suitable as a future independent proof oracle, not a runtime dependency; UFO² remains experimental after the direct UIA baseline.

## Explicitly unproven / next Windows batch

The current branch does not yet claim full Batch 3 completion. Remaining Windows-specific evidence includes: dedicated modal-window blocking/recovery, richer SelectionItem/ExpandCollapse fixture actions, process-tree/restart fixture scenarios, explicit DPI-scale matrix evidence, a comparable raw-IUIAutomation measurement, WPF/WebView2 cross-framework proof, and eventual packaged Product Journey/NVDA human testing. UAC secure desktop, higher-integrity targets, CAPTCHA/security controls and fundamentally broken UIA remain unsupported and must block unless a separately approved fallback exists.

HUMAN_TESTED=false. NVDA_VERIFIED=false.
