# PROJECT STATUS — Nika Core

Updated: 2026-08-19
Canonical repository: `Oleksii-debug/Nika-Core`
Development mode: M12 PRE-HUMAN RELEASE FREEZE

## Proven progress
- M0–M10: GREEN / INTEGRATED.
- M11 Windows packaging/distribution: GREEN / INTEGRATED / PACKAGED candidate evidence.
- M12 automated pre-human QA/release gate: GREEN / INTEGRATED; HUMAN GATE OPEN.
- Overall acceptance-gate proven progress: **98.0%**.

## Current exact human-test candidate
The previous artifact `NikaCore-0.0.2-m12-prehuman-d7bdfd697819adf13ad7423726a004fd781d857d` is **SUPERSEDED** and must not be used for human acceptance. A release audit found that its bundle lacked complete third-party license/notices evidence required by the repository release gate.

Current candidate:
- exact source head: `843d81b300fdfd031568e981cef600fbd0f8defa`;
- repair PR: #38, merged as `8065cc3fedb63f9c07e1773acf2332b5709560da`;
- Core CI #225: SUCCESS on Ubuntu + Windows;
- M11 Windows Release Candidate run #7: SUCCESS;
- M12 Pre-Human Release Gate run #5: SUCCESS;
- artifact: `NikaCore-0.0.2-m12-prehuman-843d81b300fdfd031568e981cef600fbd0f8defa`;
- artifact id: `9355471771`;
- artifact size: `20,903,072` bytes;
- workflow artifact digest: `sha256:64c99f62bdde1eafe89074893e3c3ef97adbdba1d815c77e4286193d0376146c`;
- artifact expiry observed: 2026-11-17.

## What the repaired release gate proves
- standalone PyInstaller one-dir Windows package is built;
- package launches through the existing pywebview/WebView2 shell without requiring a user-installed Python runtime;
- bundled local UI assets are included;
- deterministic release manifest records file size + SHA-256 and detects missing/unexpected/modified files;
- `THIRD_PARTY_NOTICES.txt` is generated from the exact Windows build environment before manifest generation;
- Python runtime license plus the declared/license-file evidence for the packaged runtime dependency closure are included;
- notice generation fails closed if a required runtime distribution or license evidence is missing;
- the notice file is itself covered by the release manifest SHA-256;
- M11 artifact exposes ZIP + manifest + notices;
- complete M12 Ubuntu and Windows system proofs passed;
- packaged Windows WebView2/UIA descendant discovery and Action Registry keyboard/focus proof passed;
- machine-readable M12 evidence schema v2 records `third_party_notices_verified=true` only after the release build succeeds.

## Defect found and repaired this cycle
Release audit downloaded and inspected the prior human-bound artifact. Its inner Windows ZIP contained pywebview, Python.NET, clr-loader and other runtime components, but the only discoverable package license file was Pydantic's. This contradicted the repository release gate requiring manifest/checksums/license/security evidence.

Repair #38 added deterministic third-party notice generation and verification, integrated the notices into manifest integrity, added fail-closed release-gate tests, and exposed notices in M11/M12 artifacts. The first repair candidate correctly failed because the notice verifier normalized `Python runtime` incorrectly. That verifier defect was repaired without weakening the gate, and the runtime dependency closure was expanded based on actual PyInstaller/Windows build evidence. Exact repaired head `843d81b300fdfd031568e981cef600fbd0f8defa` then passed all three required gates.

## Release truth
- HUMAN_TESTED: **false**.
- NVDA_VERIFIED: **false**.
- PRODUCTION_RELEASE_READY: **false**.
- Automation must never set HUMAN_TESTED or NVDA_VERIFIED.
- The only weighted blocker is human Windows/NVDA execution of `docs/M12_HUMAN_NVDA_ACCEPTANCE.md` against the **current exact candidate above**.

## Release-freeze rule
Do not expand production features or rebuild the candidate merely to keep automation busy. If human testing reports a concrete defect, create one new coherent repair candidate, rerun Core CI + Windows package proof + the complete M12 pre-human gate, and bind the next human test only to that exact new artifact.

Do not create status-only PR chains whose sole purpose is recording that a previous status-only PR merged.
