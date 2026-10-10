# M12 human Windows/NVDA acceptance evidence record

> Template only. Copy this file for a real human run and replace every placeholder from primary evidence. This template itself grants **no** acceptance credit.

## Decision state — must start here

- `HUMAN_TESTED`: `false`
- `NVDA_VERIFIED`: `false`
- Functional human evidence state: `NOT_RUN`
- NVDA evidence state: `NOT_RUN`
- Overall human result: `NOT_RUN`

Do not prefill either truth flag with `true`. Automation, CI, an LLM, a UIA dump or a generated report cannot change either human-only flag to `true` or change a human observation from `NOT_RUN` to `PASS`.

## A. Exact candidate identity

- Git commit SHA (40 chars): `<required>`
- Source branch expected for release: `<required>`
- Selected/current release `main` SHA immediately before the human run: `<required when release policy binds to main>`
- Governing release policy requires artifact `source_sha == selected/current main`: `NO / YES`
- Artifact/source SHA satisfies that required-main rule: `NO / YES / NOT_APPLICABLE`
- Product version: `<required>`
- M12 GitHub Actions workflow run ID: `<required>`
- M12 workflow head SHA: `<required>`
- Artifact name: `<required>`
- GitHub Actions artifact digest (`sha256:...`): `<required>`
- Downloaded artifact ZIP SHA-256, if locally hashed: `<required-or-explain-unavailable>`
- `release-manifest.json` schema version: `<required>`
- `release-manifest.json` `source_sha`: `<required>`
- Extracted `NikaCore.exe` SHA-256: `<required>`
- Candidate checked against the current release-identity policy immediately before run: `NO / YES`
- Identity reconciliation result: `NOT_RUN / PASS / FAIL / BLOCKED`
- Identity notes: `<required if not PASS>`

### Identity invariant

The Git commit SHA, M12 workflow head SHA, artifact identity and manifest `source_sha` must refer to the same exact release candidate. If the governing release policy requires the artifact source SHA to equal selected/current `main`, that equality is also mandatory at the moment the human run begins. If any required identity does not reconcile, stop: overall result is `BLOCKED`, both human-only truth flags remain `false`, and no current-candidate human evidence may be promoted to `PASS`.

## B. Required automated evidence for this exact candidate

Record only evidence actually tied to the candidate above.

- Core CI run ID/result: `<required>`
- M11 Windows Release Candidate run ID/result: `<required>`
- M12 Pre-Human Release Gate run ID/result: `<required>`
- Other release-required gate IDs/results: `<list or NONE>`
- All required automated gates refer to the exact candidate: `NO / YES`
- Automated evidence reconciliation result: `NOT_RUN / PASS / FAIL / BLOCKED`

Automated green is a prerequisite/supporting signal only. It does not set `HUMAN_TESTED=true`, `NVDA_VERIFIED=true`, or any human observation to `PASS`.

## C. Human test environment

- Human tester: `<name/identifier chosen by tester>`
- Test date/time and timezone: `<required>`
- Windows edition/version/build: `<required>`
- Architecture: `<required; expected x64>`
- NVDA version: `<required>`
- WebView2 Runtime version, if available from release evidence/environment: `<record-or-explain-unavailable>`
- Windows account privilege: `<normal-user/admin; normal-user expected>`
- Extracted package path: `<required>`
- Path contains a space: `NO / YES`
- Path contains non-ASCII characters: `NO / YES`
- Python intentionally absent/not required for package launch: `NO / YES`
- Environment notes: `<optional>`

## D. Human scenarios

Use only: `PASS`, `FAIL`, `BLOCKED`, `NOT_APPLICABLE`.

For every `FAIL` or `BLOCKED`, record the exact NVDA announcement/behavior and the control/action involved. `NOT_APPLICABLE` requires a release/spec reference; it is not a substitute for a missing claimed capability.

### D1. Keyboard-only launch and host discovery

- Result: `NOT_RUN`
- NVDA announces meaningful Nika Core app/window identity: `<observation>`
- Initial keyboard focus is understandable/recoverable: `<observation>`
- Major claimed surfaces discoverable by headings/landmarks/focus navigation: `<observation>`
- Required controls have meaningful accessible names: `<observation>`
- Tab/Shift+Tab has no focus trap or unnamed duplicate-control blocker: `<observation>`
- Notes: `<optional>`

### D2. Command/task product journey

- Result: `NOT_RUN`
- Harmless deterministic request used: `<required>`
- Command/edit field announced meaningfully: `<observation>`
- Standard editing keys remain usable: `<observation>`
- Submission reaches real product path rather than placeholder/mock-only result: `<observation>`
- Status/progress/result is readable semantic text: `<observation>`
- Pause/resume/stop semantics, if claimed: `<observation-or-NOT_APPLICABLE+reference>`
- Harmless validation/error path remains readable and focus-safe: `<observation>`
- Notes: `<optional>`

### D3. Navigation/keymap

- Result: `NOT_RUN`
- `Alt+1` behavior where exposed: `<observation-or-NOT_APPLICABLE+reference>`
- Keyboard settings reachable without mouse: `<observation>`
- Change/clear/restore shortcut where exposed: `<observation-or-NOT_APPLICABLE+reference>`
- Export/import where exposed: `<observation-or-NOT_APPLICABLE+reference>`
- Duplicate binding rejection/reporting: `<observation-or-NOT_APPLICABLE+reference>`
- Standard edit keys rechecked afterward: `<observation>`
- Notes: `<optional>`

### D4. Approval/cancel semantics

- Result: `NOT_RUN`
- Safe approval prompt used, if present on claimed path: `<description-or-NOT_APPLICABLE+reference>`
- Proposed action and choices discoverable by NVDA: `<observation>`
- Decline/cancel works by keyboard: `<observation>`
- Cancellation/denial feedback is readable and focus returns predictably: `<observation>`
- No destructive/high-impact action approved for this test: `NO / YES`
- Notes: `<optional>`

### D5. ProductProject/full-product journey — mandatory when claimed by release

- Release claims this journey: `NO / YES`
- Governing release/spec reference: `<required if NO or NOT_APPLICABLE>`
- Result: `NOT_RUN`
- ProductProject created/selected through packaged semantic UI/command path: `<observation>`
- Goal/state readable by keyboard/NVDA: `<observation>`
- Safe deterministic local project/runtime action completed: `<observation>`
- Progress/result semantic and readable: `<observation>`
- Project/durable state survives close/reopen: `<observation>`
- Notes: `<optional>`

### D6. Accessibility Repair scenario — when exposed

- Result: `NOT_RUN`
- Workspace exposed by exact candidate: `NO / YES`
- Explanation/provenance output textual and reviewable: `<observation-or-NOT_APPLICABLE+reference>`
- Semantic evidence/fallback behavior acceptable where human-observable: `<observation-or-NOT_APPLICABLE+reference>`
- No dangerous external action approved: `NO / YES`
- Notes: `<optional>`

### D7. Restart/persistence

- Result: `NOT_RUN`
- Safe state recorded before exit: `<required>`
- App closed normally by keyboard: `<observation>`
- Same exact executable reopened standalone: `<observation>`
- NVDA rediscovers host/controls after restart: `<observation>`
- Expected durable state restored where contract requires it: `<observation>`
- Recovery/error text, if any, is semantic/readable: `<observation>`
- Notes: `<optional>`

## E. Failures/blockers

For each failure/blocker add an entry.

### Item `<number>`

- Scenario/step: `<required>`
- Result: `FAIL / BLOCKED`
- Exact control/action: `<required>`
- Exact NVDA announcement or observed behavior: `<required>`
- Expected behavior: `<required>`
- Reproduction steps using keyboard: `<required>`
- Candidate identity affected: `<SHA/artifact>`
- Issue/repair reference: `<if created>`

## F. Final human declaration

Complete only after all mandatory scenarios have been executed.

- Mandatory functional scenarios contain no `FAIL`, `BLOCKED`, or unjustified `NOT_APPLICABLE`: `NO / YES`
- Mandatory NVDA/accessibility scenarios contain no `FAIL`, `BLOCKED`, or unjustified `NOT_APPLICABLE`: `NO / YES`
- Tested candidate identity remained exact and current under the governing release policy: `NO / YES`
- Test was performed by a real person using NVDA on real Windows: `NO / YES`

Final evidence states:

- Functional human evidence state: `NOT_RUN / PASS / FAIL`
- NVDA evidence state: `NOT_RUN / PASS / FAIL`
- Overall human result: `NOT_RUN / PASS / FAIL / BLOCKED`

Final truth flags:

- `HUMAN_TESTED`: `false / true`
- `NVDA_VERIFIED`: `false / true`
- Human tester declaration/notes: `<required for PASS or FAIL>`

Set `HUMAN_TESTED=true` only if the mandatory functional human evidence is `PASS` on this exact eligible candidate. Set `NVDA_VERIFIED=true` only if the mandatory NVDA evidence is `PASS` in that same real-human NVDA run. Otherwise the respective flag remains `false`.

A `PASS` in this record is valid only for the exact candidate identified in section A. Any later change that makes that candidate stale under the governing release policy — including any `main` SHA movement when exact current-main identity is required — or any later product/package-input change requires a new eligible package identity and a new human record.
