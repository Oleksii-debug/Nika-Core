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

`84998ea814fc5f99489ad583ab052b06cab2f12b`

Integrated Product Factory foundation:
- PF5 #90 — command/presentation routing;
- PF1 #91 — durable ProductProject + Research→Product handoff;
- PF2 #92 — Dynamic Team Composer + ProductRepositoryGraph;
- PF2 #93 — deterministic coordinator/reconciliation;
- PF2 #94 — public CodingWorkerPort adapter;
- PF2 #97 — restart recovery for in-flight component work;
- PF3 #95 — ExecutionNode + deployment/staging/health/rollback foundation;
- PF5 #96 — ProductProject create/inspect/update plus PF2/PF3 execution/deployment presentation, exact head `5beb29c70ee0e5e72f729ad555a49384b3308c9c`, Core #681 + M12 #449 green, integrated as merge `be7b4ab4abd59a3e373ad48562e023b07febc98c`;
- PF2 #98 — ProductProject-version-bound coordinator checkpoints, exact head `bba309b4713d385e6b7a653fffbdec313866d499`, Core #686 + M12 #454 green, integrated as merge `84998ea814fc5f99489ad583ab052b06cab2f12b`.

## Scheduled Product Factory dependency order

`PF1 → PF2 → PF3 → PF4 → PF5`

PF5 runs downstream last and consumes only integrated upstream contracts.

### AUTO-PF1 — ProductProject
PF1 #91 is **INTEGRATED**.

Available downstream: durable create/get/update-spec, optimistic concurrency and Research→Product handoff. A durable public product-decision write API is not yet integrated; PF5 must fail closed instead of writing PF1 tables directly.

### AUTO-PF2 — orchestration
PF2 #92/#93/#94/#97/#98 are **INTEGRATED**.

The integrated surface now also binds ProductProject `spec_version` + `row_version` to coordinator checkpoints and rejects stale restore. PF5 does not duplicate this persistence/recovery boundary.

### AUTO-PF3 — execution/deployment/credentials
PF3 #95 is **INTEGRATED**. Public downstream-safe surface includes execution nodes/capabilities/resources/leases, normalized exact release evidence, deployment snapshots, staging-first policy state, health and rollback evidence.

Open follow-up #99, current compatibility head `50a7083f6931e240b1b529073aba47138999188a`, remains **NOT INTEGRATED** while fresh Core #695/M12 #463 run. It owns opaque Credential/Identity Broker work; PF5 does not import its unmerged contracts or handle raw credentials.

### AUTO-PF4 — acceptance QA
PF4 remains the independent PF0–PF12 gatekeeper and evidence lane. It rejects stale/false exact-SHA evidence without becoming a competing feature writer.

### AUTO-PF5 — command journey + release
PF5 #90 and #96 are **INTEGRATED**.

Current real PF5 code/evidence PR: #100, `auto-pf5/project-scoped-command-center`, rebuilt linearly from exact current main `84998ea814fc5f99489ad583ab052b06cab2f12b`. Rollback history of the previous candidate is preserved at `backup/auto-pf5-100-118e41d9`.

Current coherent scope:
- one `ProductCommandCenter` composes integrated PF1/PF2/PF3 textual presentation;
- PF2 coordinator snapshot identity and every work record must match the inspected ProductProject or fail closed;
- global PF3 execution snapshots are reduced to target-project leases and only nodes serving those leases;
- global PF3 deployment snapshots are reduced to target-project records/environment state;
- blocker count is recomputed after composition;
- duplicate `(status kind, item_id)` identities fail closed instead of creating ambiguous semantic presentation;
- provider credential references remain absent;
- focused tests attack cross-project coordinator, execution and deployment leakage plus duplicate identity corruption.

Superseded candidate `118e41d9866e6e872ee3a91b5f827c0eb4fe4013` passed Core Ubuntu/Windows and M12 Ubuntu/Windows integrated verification before main advanced through PF2 #98; it receives no merge credit after the rebuild. Only fresh Core/M12 on the final current-main #100 SHA count.

State: **IMPLEMENTED / CURRENT-MAIN REBUILD / FRESH EXACT-HEAD GATES REQUIRED / NOT INTEGRATED**.

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

The expense application is an acceptance scenario, not a hard-coded Core product. Do not promote a new human candidate from isolated PF5 backend/presentation work.

## Next dependency-ordered integration wave

1. PF5 finishes rebuilt #100 fresh exact-head Core/M12 and live-main compatibility before merge.
2. PF1 adds a durable public decision-write boundary before PF5 can claim create/inspect/update/decision completeness.
3. PF3 proves/integrates #99 independently; PF5 consumes only integrated public credential/identity contracts.
4. PF5 may then present integrated PF2 checkpoint identity and later PF3 credential-reference state without duplicating owners.
5. Shared semantic UI waits for DEV04 ownership release plus compatibility decision.
6. PF11 package/release follows only after the representative integrated journey exists.

Progress is evidence-based; no invented Full Product Vision percentage is assigned.
