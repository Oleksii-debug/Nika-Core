# M12 human Windows/NVDA acceptance protocol

Status: final **human-only** Windows/NVDA acceptance protocol for an exact packaged Nika Core candidate.

Automated CI may prove packaging, UI Automation discovery, keyboard/focus behavior and release integrity, but automation must never set `HUMAN_TESTED` or `NVDA_VERIFIED`. A human pass is evidence only for the exact package identity recorded below; it is not transferable to a later source or package revision.

## 1. Scope and accounting

This protocol closes only the human Windows/NVDA gate for the exact candidate that is actually tested.

- Passing this protocol does **not** manufacture credit for an unproven Full Product Vision, Product Factory, Business Factory or other product journey.
- If the release candidate claims one of those user-facing capabilities, the corresponding integrated product journey and automated gates must already be green on the same exact source state before the human run can be used as final release evidence.
- A docs-only clarification does not by itself prove or change packaged behavior, and this protocol never infers that an older artifact remains current merely because executable bytes appear unchanged. Candidate reuse is governed by the canonical release-identity policy and integration decision. If the release requires artifact `source_sha` to equal the selected/current `main`, any merged commit that moves `main` changes that identity and requires a freshly qualified candidate. Product, package-input, workflow, dependency or release-manifest changes always require a fresh candidate and fresh human evidence.

## 2. Candidate identity — record before launching

The tester must obtain all of the following from the candidate that will actually be run:

- exact 40-character Git commit SHA;
- GitHub Actions workflow run ID that produced the M12 artifact;
- artifact name;
- artifact SHA-256 digest reported by GitHub Actions;
- product version;
- `release-manifest.json` schema version;
- `release-manifest.json` `source_sha`;
- SHA-256 of the downloaded artifact ZIP, when the downloaded archive is locally available for hashing;
- SHA-256 of `NikaCore.exe` after extraction.

The Git commit SHA, artifact name identity, M12 workflow head SHA and manifest `source_sha` must describe the same candidate. A mismatch is an immediate **BLOCKED/FAIL**; do not continue and do not set either human-only state to true.

Use `docs/evidence/M12_HUMAN_NVDA_RECORD_TEMPLATE.md` for the record. Its initial states are deliberately `NOT_RUN`.

## 3. Stale/superseded candidate fail-fast rule

Immediately before the human run, verify that the candidate is still the intended release candidate.

Do not award human acceptance if any of the following is true:

- a later product/package-input change has superseded the candidate;
- the governing release policy requires the candidate source SHA to equal the selected/current `main`, and `main` has moved to a different SHA, including by a docs-only commit;
- the artifact came from a feature/repair branch when the release claim requires integrated `main`;
- the current release decision refers to a different source SHA or artifact digest;
- the package/manifest identity cannot be reconciled exactly;
- the artifact was rebuilt or replaced without a new verifiable identity;
- required automated Core/M11/M12 gates for this candidate are missing, failed or were run against a different source state.

A stale artifact may be investigated, but its result must remain `HUMAN_TESTED=NOT_RUN` / `NVDA_VERIFIED=NOT_RUN` for the current release candidate.

## 4. Preconditions

- Windows 11 x64 on a real Windows desktop session.
- A real human tester operating with NVDA; automated UIA/Playwright/pywinauto results are not a substitute.
- Record the exact Windows edition/version/build and NVDA version before the run.
- Use the tester's normal non-administrator Windows account and normal NVDA profile unless a specific release requirement says otherwise.
- Extract the standalone Nika Core ZIP to a normal writable user folder. Prefer a path that includes both a space and non-ASCII characters at least once during final release acceptance, for example a user-created folder named `Nika тест 1`.
- Do not install Python for the acceptance path; the release candidate must run standalone.
- No API keys or cloud provider credentials are required for the safe acceptance path.
- Keep the evidence record open in an accessible text editor so observations can be written immediately.

## 5. Keyboard-only launch and host discovery

1. Launch `NikaCore.exe` from the extracted folder using an accessible command shell or Windows Explorer without using the mouse.
2. Confirm NVDA announces a Nika Core application/window rather than an unlabeled, blank or generic host.
3. Confirm the initial focus location is understandable and recoverable with ordinary keyboard navigation.
4. Navigate by headings/landmarks and focus navigation. Confirm the major surfaces exposed by the exact build are discoverable with meaningful names.
5. Tab and Shift+Tab through the primary controls. Every required interactive control must have an understandable accessible name and keyboard operation; no required function may be mouse-only.

Fail the relevant step if focus becomes trapped, disappears from the keyboard path, lands repeatedly on unnamed duplicate controls, or the tester must infer state from visual placement/color alone.

## 6. Command/task product journey

Use only a harmless local/deterministic task that requires no secret and no high-impact external action.

1. Focus the create-task/command surface (including `Ctrl+Shift+P` where exposed) and confirm NVDA announces the editable control and its purpose.
2. Verify ordinary text-editing keys remain ordinary editing keys in the field: arrows, Home/End, Ctrl+A, Ctrl+C/Ctrl+V where applicable, Backspace/Delete and undo where exposed. Application shortcuts must not silently steal standard text editing.
3. Submit a harmless deterministic request through the real UI.
4. Confirm the resulting status/progress/error is available as readable semantic text and can be reviewed without relying on animation, color or transient visual-only feedback.
5. If pause/resume/stop controls are exposed for the task, exercise the safe controls and confirm their names/state changes are announced meaningfully.
6. Trigger one harmless validation/error path. Confirm the error remains readable long enough to review and that focus is not lost into an inaccessible state.

A placeholder response, dead control, mock-only result or backend-only proof does not pass the human product-journey step.

## 7. Navigation and keymap checks

1. Use `Alt+1` where the exact build exposes that binding; confirm focus reaches the intended Tasks heading/region and NVDA announces the destination meaningfully.
2. Open the Keyboard settings surface using keyboard navigation.
3. Where the exact build exposes these functions, change one application shortcut, clear it, restore it, and exercise export/import.
4. Confirm duplicate binding is rejected or clearly reported.
5. Return to an editable field and re-check standard editing keys after the keymap operation.

If a control described here is intentionally absent from the exact release scope, record `NOT_APPLICABLE` with the reason and the governing release/spec reference. Do not silently treat a missing claimed capability as `NOT_APPLICABLE`.

## 8. Approval and safe-error semantics

Where the exact build exposes an approval prompt on the safe acceptance path:

- confirm NVDA can discover the prompt, proposed action and available choices;
- confirm the tester can decline/cancel entirely by keyboard;
- decline/cancel the action; do not approve a destructive or high-impact action merely for acceptance testing;
- confirm cancellation/denial produces readable feedback and returns focus to a predictable location.

Do not enter real secrets, payment credentials, financial accounts, real-money trading/gambling instructions or production publishing/send/delete targets.

## 9. ProductProject/full-product journey when claimed by the release

If the exact release candidate claims the Full Product Vision / Autonomous Product Factory representative user journey, the human run must additionally verify through the packaged UI, without manual source-code editing:

1. create or select a ProductProject using the semantic command/UI surface;
2. inspect its goal/state in readable text;
3. perform a safe deterministic local action that exercises the real project/runtime path;
4. confirm progress/result is represented semantically and not as visual-only state;
5. close Nika Core normally;
6. reopen the same exact candidate;
7. confirm the ProductProject and relevant durable state can still be found and understood by keyboard/NVDA.

If the release claims this journey but the packaged UI cannot perform it, record **FAIL**. Do not downgrade the step to `NOT_APPLICABLE` merely because backend classes or tests exist.

## 10. Accessibility Repair scenario

Where the current build exposes the Accessibility Repair/Assistant workspace:

- verify explanation/provenance output is textual and reviewable;
- verify semantic DOM/UIA evidence is preferred before any visual/coordinate fallback where that behavior is exposed to the tester;
- do not approve a dangerous external action merely for this acceptance test.

## 11. Restart/persistence check

1. Before exit, note at least one safe piece of user-visible state created during this run (for example a task/project entry or allowed preference).
2. Close Nika Core normally by keyboard.
3. Reopen the exact same `NikaCore.exe` without Python installed.
4. Confirm the application starts, NVDA can rediscover the host and controls, and the expected durable state is still available where persistence is part of the capability contract.
5. Confirm no recovery/error message is visual-only and no stale duplicate control blocks navigation.

## 12. Human observation rules

For each numbered scenario in the evidence record, write one of:

- `PASS` — the human tester executed the step and observed the required result;
- `FAIL` — the step executed but behavior violated the requirement;
- `BLOCKED` — the step could not be executed because the exact candidate/environment/evidence was invalid or unavailable;
- `NOT_APPLICABLE` — only when the exact release scope explicitly excludes that optional scenario, with a reason/reference.

Record the exact NVDA announcement/behavior for every `FAIL` or `BLOCKED` item. Never convert automated evidence into a human observation.

## 13. Final decision

`HUMAN_TESTED=true` may be recorded only when a person completed all mandatory functional scenarios on the exact candidate and no mandatory item is `FAIL`, `BLOCKED` or unjustified `NOT_APPLICABLE`.

`NVDA_VERIFIED=true` may be recorded only when that same human run used real NVDA and all mandatory NVDA/accessibility scenarios passed on the same exact candidate.

Automation, CI, a test script, an LLM, a generated report, a UIA tree dump or a reviewer reading somebody else's logs may validate supporting evidence but may **not** set either human-only state to true.

If either human-only decision fails, the rejected artifact must not be reissued as if it were fresh. A product defect requires a repaired candidate and complete applicable automated release gates before a new human run. An evidence/identity mismatch requires a correctly identified candidate before testing resumes.

## 14. Current truth before a real human run

Until a completed human evidence record exists for the selected exact candidate:

- `HUMAN_TESTED=false` / evidence state `NOT_RUN`;
- `NVDA_VERIFIED=false` / evidence state `NOT_RUN`.

Do not change those truths merely because M12 automated pre-human CI is green.
