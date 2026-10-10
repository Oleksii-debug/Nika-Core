# M12 human Windows/NVDA acceptance protocol

This is the final **human-only** Windows accessibility and packaged-user-journey gate for Nika Core V0.1. Automated CI may prove packaging, semantic HTML, UI Automation discovery, keyboard focus, durable state and release integrity, but automation must never set `HUMAN_TESTED` or `NVDA_VERIFIED`.

The protocol tests the actual packaged product a keyboard user receives. Do not substitute a Python launch, browser-only page, source checkout, unit test or older ZIP.

## 1. Candidate identity — record before launching

Record all of the following before the first human step:

- exact integrated `main` commit SHA;
- Nika Core version;
- GitHub Actions artifact name;
- artifact SHA-256/digest;
- `release-manifest.json` `source_sha`;
- M12/pre-human evidence identity;
- Windows version/build;
- NVDA version.

The commit SHA in the manifest must equal the exact candidate SHA. If the source, package inputs or generated ZIP change afterward, stop and repeat this protocol on the new candidate. A rejected or superseded artifact cannot inherit a previous human pass.

## 2. Preconditions and safety boundary

Use:

- Windows 11 x64;
- a normal, non-administrator Windows user;
- a current NVDA installation;
- the standalone Nika Core ZIP extracted to a normal writable user folder;
- no separately installed Python;
- controlled, disposable test files only.

For the local-model route, the supported V0.1 test route is local Ollama at `http://localhost:11434`. Use a locally installed test model such as `qwen3:8b` only when the candidate is being tested for live model execution. Do not download a large model merely to complete this protocol.

Do **not** enter real API keys, passwords, browser cookies, payment credentials, financial accounts, production tokens or private customer data. An external API test, if required, must use a disposable test credential supplied through the documented `env:NAME` mechanism; never paste the secret into the Nika UI.

Do not approve destructive send/delete/publish/high-impact actions merely for acceptance testing.

## 3. Clean packaged launch

1. Extract the candidate ZIP to a new folder. Do not run from inside the ZIP.
2. Using only the keyboard, launch `NikaCore.exe`.
3. Confirm Nika starts without Python installed and without administrator elevation.
4. Confirm NVDA announces a Nika Core application/window rather than an unlabeled, blank or generic WebView host.
5. Close Nika normally and launch the same EXE again. Startup must remain accessible and must not create a second visible desktop UI.

**Fail** if the packaged EXE cannot be launched keyboard-only, requires Python/admin unexpectedly, or presents an unlabeled/empty host.

## 4. Semantic structure and focus

Using NVDA browse/focus navigation and Tab/Shift+Tab:

1. Discover the main landmark and headings.
2. Confirm meaningful headings/regions exist for at least:
   - Windows autostart;
   - model settings;
   - team sources;
   - command;
   - tasks;
   - current team task;
   - ProductProject;
   - agents;
   - workspaces;
   - activity log;
   - keyboard settings.
3. Tab through every interactive control in document order.
4. Each control must have an understandable accessible name and role.
5. Focus must not become trapped in WebView2 and must not jump because background state refreshes.
6. Important state/error information must be readable text, not color-only truth.
7. While focus is in an edit field, verify standard editing keys such as arrows, Home/End, Backspace/Delete and Ctrl+A/C/X/V remain ordinary editing commands.

Record the exact control/announcement if any name, role, state or focus transition is wrong.

## 5. Keyboard action-registry behavior

1. Focus a non-editable control and use `Alt+1`; focus must move to the Tasks heading/region.
2. Use `Ctrl+Shift+P`; focus must move to “Що має зробити Nika?”.
3. Open the Keyboard section.
4. Change one non-critical Nika shortcut.
5. Clear a shortcut that is allowed to be unbound.
6. Restore its default.
7. Export the keymap and verify the JSON is available as editable text.
8. Re-import the unchanged export.
9. Attempt one duplicate binding. The conflict must be rejected or clearly reported without silently overriding another action.
10. Restore the original keymap state before continuing.

## 6. Provider/model selection

This section is mandatory once the model-selection UI is part of the candidate. If the final V0.1 candidate does not expose these controls, record a failure rather than skipping them.

### 6.1 Local Ollama route

1. Navigate to “Модель для нових завдань”.
2. Set “Тип маршруту моделі” to “Локальний Ollama”.
3. Confirm the provider identity is fixed to `ollama` and the API credential field is unavailable for this route.
4. Enter a local test model name, for example `qwen3:8b`.
5. Confirm the base address is `http://localhost:11434` or the documented equivalent loopback address.
6. Set a bounded timeout and activate “Зберегти модель”.
7. Confirm a persistent textual success message.
8. Move focus elsewhere, wait for several background refresh cycles and confirm focus is not stolen.
9. Close and reopen Nika. Confirm the route/provider/model/timeout are restored.

### 6.2 Unsaved/concurrent/error behavior

1. Change the model name but do not save. Wait for background refresh. The unsaved value must remain and must be identified as not yet saved.
2. Activate “Перечитати модель”. The UI must explicitly replace the draft with saved truth.
3. For the API route, enter an intentionally invalid credential reference such as `not-an-env-reference`. Saving must fail with a safe readable validation message.
4. Never paste a real API key. The UI must describe the field as an `env:NAME` reference.
5. If a configured API route is tested, after reopen the UI may state that a credential reference is configured, but it must **not** reveal the stored reference or the secret value.
6. Disconnect/fail the provider in a controlled way. The UI must show a persistent safe failure/offline state and must not claim success or silently switch providers.

## 7. Team-source setup

Use two controlled small files with different content.

1. Navigate to “Джерела команди”.
2. Enter the full source-folder path.
3. Enter the first and second file names/paths.
4. Activate “Зберегти джерела”.
5. Confirm the success message and that focus moves to “Що має зробити Nika?”.
6. Edit one source field without saving and wait for background refresh. The draft must not disappear.
7. Activate “Перечитати збережені” and confirm the saved values return.
8. Close/reopen Nika and confirm the saved source configuration is restored.

Paths containing spaces and Ukrainian characters must work. An absent file, same-file pair, unsupported file or path outside the allowed source root must fail before task creation with readable safe text.

## 8. Representative Start → real three-agent progress → result

1. With the intended model route and two controlled sources configured, enter a harmless comparison request.
2. Activate “Створити завдання” using the keyboard.
3. The command action may say the task was accepted; it must not falsely say the task itself completed at dispatch time.
4. Confirm focus moves to the Tasks region.
5. Inspect “Командне завдання”.
6. Confirm the displayed roster/progress comes from durable backend state and represents the real V0.1 team; a missing member must not be invented merely to show “three”.
7. Confirm member state/current operation and safe team events are readable with NVDA.
8. During progress updates, keep focus on another control for several refresh cycles. Background refresh must not steal focus.
9. Wait for terminal completion of the controlled task.
10. Confirm the final result and terminal team/task state are textual and mutually consistent.
11. Close/reopen Nika. The completed task/result must still be visible without rerunning the already completed work.

If the candidate claims live model-backed execution, this same representative task must use the task’s frozen model route as proven by the candidate’s automated exact-SHA evidence. Merely saving a model name while a different runtime executes the task is not sufficient release evidence.

## 9. Pause / Resume / Cancel

Run this section only on a candidate that includes the release-bound active Pause/Resume implementation. If the control exists but active durable pause is still unsupported, that is an open V0.1 product blocker, not a human pass.

Use a harmless controlled task that remains active long enough to operate the controls.

1. Start the task.
2. Activate “Призупинити”.
3. Confirm a truthful PAUSED state. The UI must not show PAUSED while the runtime continues side effects.
4. Close Nika while the task is paused.
5. Reopen Nika. It must still be paused/manual-resume, not silently restarted and not converted to an invented success state.
6. Activate “Продовжити” once.
7. Confirm continuation occurs once; no duplicate already-confirmed work appears.
8. Start another controlled task and activate “Зупинити агента”/Cancel.
9. Confirm future work stops and the durable state becomes cancelled or an explicit safe reconciliation state.
10. Reopen Nika and confirm cancellation remains truthful.

An ambiguous unqualified control target must fail closed instead of pausing/resuming/cancelling an arbitrary task.

## 10. Offline, recovering and uncertain states

Exercise at least one controlled failure that does not involve a real secret or destructive effect.

Verify:

- offline/provider-unavailable is announced/readable and is not reported as completed;
- recovering state is distinguishable from ordinary running;
- an uncertain external-effect outcome is distinguishable from failed/completed and is not blindly retried;
- persistent errors remain available long enough to reread/copy;
- restart does not turn uncertain work into authorized/completed truth;
- no background error announcement repeatedly steals focus.

If the current V0.1 UI lacks a truthful presentation for a backend state that the product can actually enter, record that as a failure.

## 11. Windows autostart

1. Navigate to “Автозапуск Windows”.
2. Tab to “Запускати Nika разом із Windows”.
3. Toggle it with Space. Confirm the UI says the choice is not saved yet.
4. Activate “Зберегти автозапуск”.
5. Confirm the enabled state is read back as text and focus returns predictably.
6. Close and reopen Nika. Confirm the saved state is still reported.
7. Activate “Перечитати автозапуск”; it must show actual Windows state rather than an optimistic cached state.
8. Disable and save before ending the ordinary protocol, unless actual sign-in testing is being performed.

### Optional final autostart sign-in proof

If the release owner requires the real Windows sign-in event:

1. Save autostart enabled.
2. Sign out of the **test** Windows account and sign back in.
3. Confirm exactly one accessible Nika window starts without elevation.
4. Confirm autostart did not itself authorize/replay a task.
5. Disable autostart afterward.

Do not simulate this by changing the registry manually during the human acceptance run.

## 12. Package update/reopen continuity

For a release that is replacing an older accepted Nika build:

1. Keep the old package folder unchanged as rollback media.
2. Extract the new candidate to a **new** folder; do not overwrite a running installation.
3. Launch the new candidate as the same normal Windows user.
4. Confirm durable user state expected to survive upgrade is present: model/source settings, task history and other canonical profile data.
5. Confirm the old executable path is not silently retained by Windows autostart; a stale autostart registration must be reported as stale and require explicit save to bind the new EXE.
6. Confirm no duplicate database/profile is silently created merely because the launch directory changed.
7. If migration/recovery reports a conflict, stop and record the message; do not delete databases to force a pass.

Automated release/recovery evidence still owns byte-level backup, manifest and rollback proof. The human step verifies the customer-visible packaged transition.

## 13. ProductProject and readable state

1. Navigate to ProductProject, Tasks, Agents, Workspaces and Activity Log using headings/landmarks.
2. Confirm identifiers, status, blockers and progress exposed by the current candidate are readable text.
3. Confirm the UI does not fabricate unavailable scheduler/team/effect truth.
4. Trigger one harmless validation error and verify it is readable and persistent.
5. If the Accessibility Repair/Assistant workspace is present, confirm explanation/provenance is textual and semantic DOM/UIA evidence is preferred before visual/coordinate fallback. Do not authorize a dangerous external action for this test.

## 14. Pass/fail record

Record one line for every section above:

- `PASS`;
- `FAIL`;
- `NOT_APPLICABLE` only when the section explicitly permits it.

For each failure record:

- section and step number;
- exact NVDA announcement or absence of announcement;
- focused control name/role if known;
- expected behavior;
- observed behavior;
- whether restart changed the observation;
- no secrets or private source contents.

Final record:

```text
CANDIDATE_SHA=
ARTIFACT_NAME=
ARTIFACT_DIGEST=
MANIFEST_SOURCE_SHA=
WINDOWS_VERSION=
NVDA_VERSION=
HUMAN_TESTED=PASS|FAIL
NVDA_VERIFIED=PASS|FAIL
FAILED_STEPS=
NOTES_SAFE=
```

`HUMAN_TESTED=true` may be recorded only after a person completes the functional sections on the exact candidate. `NVDA_VERIFIED=true` may be recorded only after the NVDA-specific checks pass on that same candidate.

A failure blocks production V0.1 and requires a new candidate after repair. Automated UIA/DOM tests, screenshots, logs or another model’s assessment cannot set either human flag.
