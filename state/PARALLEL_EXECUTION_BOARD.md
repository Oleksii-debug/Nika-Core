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

`a1784b81da55a9bc4d139750d1a531e16e788200`

Integrated Product Factory foundation:
- PF5 #90 — command/presentation routing;
- PF2 #92 — Dynamic Team Composer + ProductRepositoryGraph;
- PF2 #93 — deterministic coordinator/reconciliation;
- PF2 #94 — public CodingWorkerPort adapter;
- PF1 #91 — durable ProductProject + Research→Product handoff;
- PF2 #97 — restart recovery for in-flight component work.

## Scheduled Product Factory dependency order

`PF1 → PF2 → PF3 → PF4 → PF5`

PF5 runs downstream last and consumes only integrated upstream contracts.

### AUTO-PF1 — ProductProject
PF1 #91 is **INTEGRATED** as merge `47aa0f7595a77f634f54de1870b0bfc3c7da66cc`.

Available to downstream: durable create/get/update-spec, optimistic concurrency and Research→Product handoff. A durable public product-decision write API is not yet integrated; PF5 must fail closed instead of writing PF1 tables directly.

### AUTO-PF2 — orchestration
PF2 #92/#93/#94/#97 are **INTEGRATED**. Current main includes restart recovery after exact Core #668 + M12 #436 green evidence.

Open follow-up #98, `auto-pf2/product-project-binding`, head `14036a6d0d484afbd68fc36c2ce746e73c5d828c`, is **RED / NOT INTEGRATED**: Core #670 and M12 #438 failed. PF5 does not consume it.

### AUTO-PF3 — execution/deployment
Open draft #95, `auto-pf3/execution-deployment-foundation`, current head `4a3e0b342ec06c936693c8f583ed4f7a4fdc2007`, is **NOT INTEGRATED**. PF5 does not import node/build/staging/health/rollback/ops contracts until merge.

### AUTO-PF4 — acceptance QA
PF4 remains the independent PF0–PF12 gatekeeper and evidence lane. It rejects stale/false exact-SHA evidence without becoming a competing feature writer.

### AUTO-PF5 — command journey + release
PF5 #90 is **INTEGRATED**.

Current real PF5 code/evidence PR: #96, `auto-pf5/command-journey-pf2-presentation`.

Current coherent scope:
- deterministic Ukrainian + English ProductProject/Toolsmith routing;
- explicit ambiguity for mixed product/capability intent;
- integrated PF2 CoordinatorSnapshot → textual ProductStatusEntry projection;
- real integrated PF1 ProductProject create/inspect/update through `ProductProjectRepository`;
- SQLite restart, stale-version and credential-reference redaction tests;
- product-decision persistence fails closed until PF1 exposes a durable public decision API;
- canonical status reconciliation in this same real code/evidence PR.

PR #96 was refreshed onto current main. Backup branch `backup/auto-pf5-96-bb24519` preserves the pre-rebase PF5 head. The final candidate must have a clean PF5-only PR diff plus fresh exact-head Core + M12. Older #96 runs do not receive final credit.

State: **IMPLEMENTED / FINAL CURRENT-MAIN LINEARIZATION + PREFLIGHT + FRESH GATES REQUIRED / NOT INTEGRATED**.

## Manual/shared ownership — no scheduled duplication

- DEV01 #86 — Research/Corpus report exports.
- DEV02 #72 — Windows worker containment proof.
- DEV03 #67 — deterministic trader replay/accounting/risk.
- DEV04 #78 — strict Windows UIA semantic vertical and shared Interaction/UIA ownership; dedicated live proof remains blocked by duplicate semantic-node identity.
- DEV05 #89 — stable platform subtitle acquisition.
- M10 #61/#62 — authorization/approval security ownership.

PF5 does not edit these production slices without an explicit compatibility decision.

## PF5 interaction/UI rule

Shared semantic Windows UI remains outside PF5 ownership while DEV04 #78 is active. PF5 advances native/API and textual presentation contracts first.

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
4. Never import an unmerged sibling branch as canonical dependency.
5. Shared-contract edits require explicit compatibility decision and focused tests.
6. A blocked upstream lane does not idle independent downstream work.
7. Exact-head acceptance + current-main compatibility are required before merge credit.
8. No direct scheduled-worker writes to `main`.

## Product Factory release policy

Backend-only tests do not close Product Factory. PF11 requires a representative product created by the real factory from a clean packaged Windows Nika installation: research, durable ProductProject, product decision, acceptance criteria, team, repository, isolated implementation, independent QA/accessibility, package/release provenance and restart/resume.

The expense application is an acceptance scenario, not a hard-coded Core product. Do not promote a new human candidate from isolated PF5 backend work.

## Next dependency-ordered integration wave

1. PF5 linearizes #96 onto current main with only PF5-owned files, runs preflight and fresh exact-head Core/M12.
2. PF1 adds a durable public decision-write boundary before PF5 can claim create/inspect/update/decision completeness.
3. PF2 repairs #98 independently; PF5 consumes it only after integration.
4. PF3 integrates #95 before PF5 exposes deploy/ops presentation.
5. Shared semantic UI waits for DEV04 ownership release plus compatibility decision.
6. PF11 package/release follows only after the representative integrated journey exists.

Progress is evidence-based; no invented Full Product Vision percentage is assigned.
