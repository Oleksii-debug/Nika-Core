# PARALLEL EXECUTION BOARD — Nika Core

Updated: 2026-08-20.
Mode: **ACTIVE AUTONOMOUS PRODUCT FACTORY DEVELOPMENT**.
Canonical technical evidence: live GitHub `main`, exact PR heads and current Actions. Drive owns automation routing/ownership/handoff truth.

## Evidence states
- PREPARED — scope/contracts/reuse decision ready.
- IMPLEMENTED — production-intended source/tests exist on a branch.
- GREEN — exact branch/PR head passed required automated checks.
- INTEGRATED — exact green candidate merged into `main`.
- PACKAGED — exact installable artifact built and checked.
- HUMAN_TESTED — a person completed the required manual protocol.
- NVDA_VERIFIED — the human NVDA protocol passed; automation may never award this state.

Current global truth:
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- `PF11=false`;
- historical Core percentages and stale Windows ZIPs are archival only.

## Canonical main

`700394d678bce7226374ee563e8e38ab76ab4538`

Integrated Product Factory foundation:
- PF5 PR #90 — command/presentation routing foundation;
- PF2 PR #92 — Dynamic Team Composer + ProductRepositoryGraph;
- PF2 PR #93 — deterministic coordinator/reconciliation;
- PF2 PR #94 — thin public CodingWorkerPort adapter, integrated from exact candidate `925e13ef349cfa10e127abacefe3ba0e329a4f77` after Core #653 + M12 #421.

## Scheduled Product Factory dependency order

Five existing scheduled lanes run dependency-first:

`PF1 → PF2 → PF3 → PF4 → PF5`

PF5 runs downstream last and consumes only integrated upstream contracts.

### AUTO-PF1 — ProductProject
PR #91 · `auto/pf1-product-project` · current head `7ed579fcb5b269c95a82b6b818bd462dcda9fb39`.

State: **RED / NOT INTEGRATED** on current exact head because Core #660 succeeded but M12 #428 failed. Older green PF1 heads are stale evidence only. PF1 must repair the exact failure family, refresh current-main compatibility and obtain fresh required green evidence before merge.

Ownership: durable ProductProject, Research→Product handoff, PF0/PF1/PF12 durability state. Other scheduled lanes do not duplicate this persistence.

### AUTO-PF2 — orchestration
PR #93 and follow-up PR #94 are **INTEGRATED**.

PF2 owns ProductRepositoryGraph/team/composition/coordinator plus the thin adapter to public CodingWorkerPort. PF5 may consume these integrated contracts but does not edit DEV02 worker/security internals.

### AUTO-PF3 — execution/deployment
Open PR #95 · `auto-pf3/execution-deployment-foundation` · head `1322547198d6b847869ed9a53a7e7964616abc32`.

Core #654 + M12 #422 succeeded on that exact head, but the PR is still open/draft and based on an older main. State: **GREEN-BUT-UNINTEGRATED / CURRENT-MAIN COMPATIBILITY REQUIRED**. PF5 must not import it until merge.

### AUTO-PF4 — acceptance QA
PF4 is the independent PF0–PF12 gatekeeper and acceptance/evidence lane. It classifies candidates as RED / GREEN-BUT-STALE / MERGE-READY / BLOCKED, rejects stale or false evidence and advances executable acceptance/security/restart proof rather than becoming a second feature writer.

### AUTO-PF5 — command journey + release
PR #90 is **INTEGRATED**.

Current PF5 real code/evidence PR #96 · `auto-pf5/command-journey-pf2-presentation`.
Starting main: `2145561509ce655adb89ed0a3e8aa027d0a7940d`.
Current compatibility main: `700394d678bce7226374ee563e8e38ab76ab4538`.

Previous exact candidate `a7e8fc9d1e05cbc398e9b5c59a557ae7939daa04` passed Core #659 + M12 #427. Main then advanced by six commits through PF2 #94. Compare showed those main-only commits touch only the PF2 coding-worker adapter and its focused test, with no overlap with PR #96's six files. This same real PR now reconciles canonical status to current main; therefore the previous green candidate is superseded and fresh exact-head gates are required.

Current PF5 scope:
- deterministic Ukrainian + English ProductProject/Toolsmith command classification;
- mixed product/capability intent fails to explicit user decision;
- ordinary commands remain AgentTask;
- integrated PF2 CoordinatorSnapshot → PF5 textual ProductStatusEntry adapter;
- component/repository/base-SHA/attempt/allowed-path/review/QA/blocker evidence;
- focused presentation/a11y/error tests;
- canonical PROJECT_STATUS/PARALLEL_EXECUTION_BOARD reconciliation in this same real PF5 code/evidence PR.

State: **IMPLEMENTED / CURRENT-MAIN COMPATIBLE AT FILE LEVEL / FRESH EXACT-HEAD GATES REQUIRED / NOT INTEGRATED**.

PF5 does not create ProductProject persistence while PF1 is unintegrated and does not import PF3 #95 branch contracts.

## Manual/shared ownership — scheduled lanes must not duplicate

- DEV01 PR #86 — Research/Corpus report exports.
- DEV02 PR #72 — Windows worker reparse/process containment proof.
- DEV03 PR #67 — deterministic trader replay/accounting/risk.
- DEV04 PR #78 — strict Windows UIA semantic vertical; dedicated live proof currently has a duplicate semantic-node identity blocker. DEV04 retains Interaction/UIA/shared semantic UI ownership.
- DEV05 PR #89 — stable platform subtitle acquisition.
- M10 PR #61 and stacked R4 PR #62 — security-sensitive authorization/approval ownership.

Scheduled PF workers consume stable integrated contracts only and do not edit these production slices without an explicit compatibility decision.

## PF5 interaction/UI rule

Shared semantic Windows UI is not edited while DEV04/shared ownership is active. PF5 advances framework-neutral API/presentation contracts and tests first.

Interaction priority:
1. native/application API;
2. DOM/UIA/accessibility semantics;
3. named deterministic controls;
4. screenshot/OCR/vision fallback;
5. coordinates last.

Automated semantic/UIA evidence never sets `NVDA_VERIFIED=true`.

## Collision policy
1. One writer per production slice.
2. Separate branch per independent coherent lane.
3. Branch from latest compatible green main unless a real dependency requires otherwise.
4. Never import an unmerged sibling branch as a canonical dependency.
5. Shared-contract edits require an explicit compatibility decision and focused tests.
6. A blocked upstream lane does not idle independent downstream contract/a11y/provenance work.
7. Exact-head acceptance + current-main compatibility are required before merge credit.
8. No direct scheduled-worker writes to `main`.

## Product Factory release policy

Backend-only tests do not close the Product Factory. PF11 requires a representative product created by the real factory from a clean packaged Windows Nika installation, including research, decision, durable ProductProject, acceptance criteria, team, repository, isolated implementation, independent QA/accessibility, package/release provenance and restart/resume.

The representative expense application is an acceptance scenario, not code to hard-code into Nika Core.

Do not build or promote a new human candidate from an isolated backend change. Package only at a meaningful integrated exact-SHA milestone and rerun the complete applicable release gate. Older ZIPs become stale after integrated behavior changes.

## Next dependency-ordered integration wave

1. PF1 repairs #91 current exact-head M12 failure, refreshes compatibility and integrates only after fresh green evidence.
2. PF3 refreshes #95 against current main and integrates only after compatibility + required exact evidence.
3. PF5 reruns fresh exact-head Core + M12 for #96 after this current-main/status reconciliation and merges only if still compatible.
4. PF5 then wires real integrated ProductProject create/inspect/update/decision and restart-aware Product Journey tests after PF1 integration.
5. After PF3 integration, PF5 exposes node/build/staging/health/rollback/ops without credential material.
6. Semantic UI wiring waits for ownership release + compatibility decision.
7. PF11 packaging/release follows only after the representative integrated journey exists.

Progress is evidence-based; no invented Full Product Vision percentage is assigned.
