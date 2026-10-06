# M12 human Windows/NVDA acceptance protocol

This document is the final human-only accessibility gate for Nika Core. Automated CI may prove packaging, UI Automation discovery, keyboard focus and release integrity, but it must never set `HUMAN_TESTED` or `NVDA_VERIFIED`.

## Candidate identity
Before testing, record the exact Git commit SHA and the GitHub Actions artifact name/digest. Test only that candidate. If source or package changes afterward, repeat this protocol against the new exact candidate.

## Preconditions
- Windows 11 x64.
- Current user NVDA installation and normal user profile.
- Extract the standalone Nika Core ZIP to a normal writable user folder.
- Do not install Python; the release candidate must run standalone.
- No API keys or cloud provider credentials are required for this acceptance path.

## Human functional and NVDA checks
1. Launch `NikaCore.exe` from the extracted folder using Windows Explorer or an accessible command shell.
2. Confirm NVDA announces a Nika Core application/window rather than an unlabeled or blank host.
3. Navigate by headings and landmarks. Confirm the Tasks, Agents, Workspaces and Logs areas are discoverable with meaningful names.
4. Tab through the interface. Confirm every interactive control has an understandable accessible name, focus is visible/announced, and no required function is mouse-only.
5. Focus “Створити завдання”. Use `Alt+1`; confirm focus moves to the Tasks heading/region and NVDA announces the destination meaningfully.
6. Use `Ctrl+Shift+P`; confirm focus moves to “Що має зробити Nika?” and ordinary text editing keys continue to behave normally inside the edit field.
7. Open the Keyboard settings surface. Verify an application shortcut can be changed, cleared, restored, exported/imported as exposed by the current UI, and that a duplicate binding is rejected or clearly reported.
8. Trigger a harmless validation/error path. Confirm the error/status is available as readable text and does not disappear before it can be reviewed or copied.
9. Inspect Tasks, Agents, Workspaces and Logs using NVDA browse/focus navigation. Confirm important state is text/semantic content rather than visual-only information.
10. Close Nika Core normally, reopen it, and confirm the application starts without Python installed and the UI remains accessible.

## Accessibility Repair scenario
Where the current build exposes the Accessibility Repair/Assistant workspace, verify that its intended explanation/provenance output is textual and that semantic DOM/UIA evidence is preferred before any visual/coordinate fallback. Do not approve a dangerous external action merely for this acceptance test.

## Safety checks
- Do not enter real secrets, payment credentials, financial accounts or real-money trading/gambling instructions.
- Do not approve destructive send/delete/publish/high-impact actions for acceptance testing.
- Confirm any approval prompt you do encounter describes the proposed action before execution and can be declined/cancelled.

## Pass/fail recording
Record:
- exact commit SHA;
- artifact name and digest;
- Windows version;
- NVDA version;
- HUMAN_TESTED: PASS or FAIL;
- NVDA_VERIFIED: PASS or FAIL;
- each failed step number with the exact NVDA announcement/behavior observed.

`HUMAN_TESTED=true` may be recorded only after the functional checklist is completed by a person on the exact candidate. `NVDA_VERIFIED=true` may be recorded only after the NVDA-specific checks pass on that same candidate. A failure blocks production v1.0 and must produce a new candidate rather than reusing the rejected artifact as if it were fresh.
