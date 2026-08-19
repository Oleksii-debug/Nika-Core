# PARALLEL EXECUTION BOARD — Nika Core

Updated: 2026-08-19
Mode: M12 PRE-HUMAN RELEASE FREEZE
Canonical progress evidence: merged PR + exact green CI + milestone acceptance evidence.

## Evidence states
- PREPARED — scope/contracts/reuse decision ready.
- IMPLEMENTED — production-intended source/tests exist on a branch.
- GREEN — exact branch/PR head passed required automated checks.
- INTEGRATED — exact green candidate merged into `main`.
- PACKAGED — installable Windows artifact built and checked.
- HUMAN_TESTED — a person completed the specified manual protocol.
- NVDA_VERIFIED — human NVDA test passed; automation may never award this state.

## Current product truth
Overall acceptance-gate proven progress: **98.0%**.

M0–M11 are GREEN / INTEGRATED. M11 additionally has automated PACKAGED candidate evidence. M12 automated pre-human QA is GREEN / INTEGRATED, but the final human Windows/NVDA gate is still open.

Human truth:
- HUMAN_TESTED: false.
- NVDA_VERIFIED: false.
- PRODUCTION_RELEASE_READY: false.

Exact M12 human-bound candidate:
- candidate head: `d7bdfd697819adf13ad7423726a004fd781d857d`;
- Core CI 200: SUCCESS;
- M12 Pre-Human Release Gate run 1: SUCCESS;
- artifact: `NikaCore-0.0.2-m12-prehuman-d7bdfd697819adf13ad7423726a004fd781d857d`;
- artifact id: `9340947647`;
- digest: `sha256:ee0ec0b7506c6bc84b8052f1c1cdb80a33a6f0dea8eb6b221cee1860dc96d4c5`.

## Integrated lanes

### L1 — M3 durable memory, scheduler, resource control
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `c9c7e105838d9af8a65341fd28f4591aee0d851c`; Core CI 98; PR #8 merged as `3b3718c214850c0211d18f520b5892c2cf47403c`.

### L2 — M4 Model Gateway, tools and MCP
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `14368c60fa8c8351e6a8776263d3d90b3e5dfb0e`; Core CI 112; PR #10 merged as `af449172064a696250399ff645ef01eb17ac6c84`.

### L3 — M5 accessible Windows UI
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `9b536af11aaa30e72c5d3562e8b2beede7e5b5b2`; Core CI 137; PR #13 merged as `6b9c023d62b30500bec50a1d9484a78cfb6aafbd`.
Automated packaged WebView2/UIA evidence exists, but HUMAN_TESTED=false and NVDA_VERIFIED=false.

### L4 — M6 Agent Builder and permissions
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `b2f5939dae432f2bb0b819b3c70adf8c9d0dafe4`; Core CI 142; PR #15 merged as `088da78b45be390fe0aab0c6d1c84c5a8f5d9d53`.

### L5 — M7 multi-agent laboratory
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `4feb976faa97949bacc321bcbce792d01359a58c`; Core CI 150; PR #17 merged as `5a01692c1372375f040cd38558e33204b082d5a5`.

### L6 — M8 controlled learning and experiments
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `d8f14ac097f2eb664b7f893543db359fb6245e6c`; Core CI 159; PR #19 merged as `a8fd7b6c094489cebb79a4221e4124b5cfbad8f6`.
Safety truth: no production-source mutation path and no silent permission widening.

### L7 — M9 Plugin SDK and real workspaces
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Foundation exact green head: `ef4bafc02d4517b3718f07e5f61b7e4e22072c25`; Core CI 188; PR #21 merged as `e8967b97ec9772cb2af0b3b781468553708d4015`.
Semantic-adapter exact green head: `680a8a15dfb5a30a7c6eaba3860817c1cbda7ac5`; Core CI 193 Ubuntu+Windows plus live Playwright ARIA and Windows UIA proofs; PR #24 merged as `e6af748fb655fda2719be93258b6c2a427287994`.
Integrated evidence: versioned plugin/workspace compatibility and risk declarations, lazy explicit activation, Software Factory behind `CodingWorkerPort`, semantic-first Accessibility Repair/Assistant, optional Playwright and Windows UIA adapters.

### L8 — M10 security/sandbox/reliability
Current state: IMPLEMENTED / GREEN / INTEGRATED.
Exact green head: `17826cc041caeaa1fe03f8dbe4ce5e50c7aecad5`; Core CI 167 Ubuntu+Windows; PR #22 merged as `976cb9914966a67763742a28c229730417ee63c8`.
Integrated evidence: traversal-safe workspace boundaries, exact network/process allowlists, bounded execution budgets, exact-action fingerprints, expiring single-use approvals and fail-closed authorization order.

### L9 — M11 Windows packaging/distribution
Current state: IMPLEMENTED / GREEN / INTEGRATED / PACKAGED candidate evidence.
Exact green head: `0f48c3c4889478f967e383d385576e9322e55c9b`; Core CI 196; M11 Windows Release Candidate run 3; PR #23 merged as `5d86d83f6ce99fa4c9bd6a0eaadef1a6b404283c`.
Artifact: `NikaCore-0.0.2-windows-x64`, id `9340673484`, digest `sha256:4611ba73baab2ecd37ce07d5596853edd6f4d6fa7559ff58d660d5f5aee5b12e`.

### L10 — M12 full-system QA/accessibility/release gate
Current state: AUTOMATED PRE-HUMAN GATE GREEN / INTEGRATED; HUMAN GATE OPEN.
Exact automated green head: `d7bdfd697819adf13ad7423726a004fd781d857d`; Core CI 200; M12 Pre-Human Release Gate run 1; PR #26 merged as `a278767de4435535cc54e5674ddc72d37051ba03`.
Exact pre-human artifact is recorded above.

## Release-freeze operating rule
Normal feature expansion is paused while human acceptance is bound to the exact M12 artifact. Automation must not mutate production source merely to keep hourly workers busy.

Safe autonomous work during the freeze is limited to:
1. artifact/evidence integrity checks;
2. canonical status/evidence consistency;
3. closing stale/superseded coordination artifacts;
4. protocol clarification;
5. investigation of concrete human-reported defects;
6. preparing one coherent repair candidate only after a real defect is demonstrated.

Do **not** create status-only PR chains whose sole purpose is to record that the previous status-only PR merged. One coherent reconciliation PR is sufficient; if no new evidence or defect exists, record no new PR.

## Collision policy
1. Source-development lanes branch from latest green `main` unless a real dependency requires otherwise.
2. Do not stack unrelated branches.
3. Shared contracts require explicit compatibility decisions and targeted tests.
4. A blocked lane does not justify weakening another lane's gate.
5. Release-freeze rules override normal feature-parallelism while the exact human candidate is pending.

## CI policy
- Coherent PR/main gates use the shared verification harness on Ubuntu and Windows where applicable.
- Focused Windows/WebView2/package/security jobs run only when they add material evidence.
- Never weaken checks to obtain green.
- Avoid rerunning expensive identical gates without new code/evidence.

## Reporting policy
Every meaningful development report records exact starting `main` SHA, lane/branch, changes, REUSE/ADAPT/CUSTOM decision, tests, exact green head, PR/merge, proven A–Z progress, blockers, unverified work and HUMAN_TESTED/NVDA_VERIFIED truth.
