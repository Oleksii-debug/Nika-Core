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

`2145561509ce655adb89ed0a3e8aa027d0a7940d`

Integrated Product Factory foundation:
- PF5 PR #90 — command/presentation routing foundation;
- PF2 PR #92 — Dynamic Team Composer + ProductRepositoryGraph;
- PF2 PR #93 — deterministic coordinator/reconciliation; exact candidate `5f84e4c3380b0834b1a9b0141fe7f3e1f3e23661` passed Core #646 + M12 #414 before integration.

## Scheduled Product Factory dependency order

Five existing scheduled lanes run dependency-first:

`PF1 → PF2 → PF3 → PF4 → PF5`

PF5 runs downstream last and consumes only integrated upstream contracts.

### AUTO-PF1 — ProductProject
PR #91 · `auto/pf1-product-project` · head `a973b82e096f642f82c2e9f53124484c5542a6f3`.

State: **GREEN-BUT-STALE / NOT INTEGRATED**. Exact head passed Core #640 + M12 #408, but current main advanced afterward. Refresh compatibility + fresh exact-head evidence is required before merge.

Ownership: durable ProductProject, Research→Product handoff, PF0/PF1/PF12 durability state. Other scheduled lanes do not duplicate this persistence.

### AUTO-PF2 — orchestration
PR #93 is **INTEGRATED**.

Open follow-up PR #94 · `auto-pf2/coding-worker-adapter` · head `925e13ef349cfa10e127abacefe3ba0e329a4f77`.

State: **IMPLEMENTED / EXACT-HEAD GATES PENDING / NOT INTEGRATED**.

PF2 owns ProductRepositoryGraph/team/composition/coordinator and the thin adapter to public CodingWorkerPort. It does not own PF1 persistence, DEV02 worker security internals or PF5 presentation.

### AUTO-PF3 — execution/deployment
Open PR #95 · `auto-pf3/execution-deployment-foundation` · head `1322547198d6b847869ed9a53a7e7964616abc32`.

State: **IMPLEMENTED / EXACT-HEAD GATES PENDING / NOT INTEGRATED**.

PF3 owns provider-neutral execution-node/deployment/staging/health/rollback/ops contracts. PF5 must not import them until integrated.

### AUTO-PF4 — acceptance QA
PF4 is the independent PF0–PF12 gatekeeper and acceptance/evidence lane. It classifies candidates as RED / GREEN-BUT-STALE / MERGE-READY / BLOCKED, rejects stale or false evidence and advances executable acceptance/security/restart proof rather than becoming a second feature writer.

### AUTO-PF5 — command journey + release
PR #90 is **INTEGRATED**.

Current PF5 real code/evidence PR #96 · `auto-pf5/command-journey-pf2-presentation`.
Starting main: `2145561509ce655adb89ed0a3e8aa027d0a7940d`.

Current scope:
- deterministic Ukrainian + English ProductProject/Toolsmith command classification;
- mixed product/capability intent fails to explicit user decision;
- ordinary commands remain AgentTask;
- integrated PF2 CoordinatorSnapshot → PF5 textual ProductStatusEntry adapter;
- component/repository/base-SHA/attempt/allowed-path/review/QA/blocker evidence;
- focused presentation/a11y/error tests;
- canonical PROJECT_STATUS/PARALLEL_EXECUTION_BOARD reconciliation in this same real PF5 code/evidence PR.

State: **IMPLEMENTED / EXACT-HEAD GATES PENDING / NOT INTEGRATED**.

PF5 does not create ProductProject persistence while PF1 is unintegrated and does not import PF2 #94 or PF3 #95 branches.

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

1. PF1 refreshes and safely integrates #91.
2. PF2 completes #94; downstream consumes only after merge.
3. PF3 completes #95; downstream consumes only after merge.
4. PF5 drives #96 to exact-head green and integrates only after current-main compatibility recheck.
5. PF5 then wires real integrated ProductProject create/inspect/update/decision and restart-aware Product Journey tests.
6. After PF2/PF3 follow-ups integrate, PF5 exposes team/repo/component/work/review/repair and node/build/staging/health/rollback/ops without credential material.
7. Semantic UI wiring waits for ownership release + compatibility decision.
8. PF11 packaging/release follows only after the representative integrated journey exists.

Progress is evidence-based; no invented Full Product Vision percentage is assigned.
