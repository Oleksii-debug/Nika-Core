# PROJECT STATUS — Nika Core

Updated: 2026-08-20.
Canonical repository: `Oleksii-debug/Nika-Core`.
Canonical technical truth: live GitHub `main`, exact PR heads and current Actions. Drive is routing/ownership/handoff truth.

## Practical product truth

Nika Core is in active Autonomous Product Factory development. Historical Core percentages and old Windows artifacts are archival evidence only; they do not prove the expanded Full Product Vision or PF0–PF12 acceptance.

Current human/release truth:
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- `PF11=false`;
- no stale ZIP may be promoted as a current Product Factory candidate.

## Canonical main

Current main at this reconciliation point:

`df84a72d6705aa78cb0c69df9e47a367098b74bb`

Integrated Product Factory foundation includes:
- PF5 PR #90 — command/presentation routing foundation;
- PF1 PR #91 — durable ProductProject + Research→Product foundation;
- PF2 PR #92 — Dynamic Team Composer + ProductRepositoryGraph;
- PF2 PR #93 — deterministic Product Factory coordinator/reconciliation;
- PF2 PR #94 — public CodingWorkerPort adapter;
- PF2 PR #97 — restart recovery for in-flight component work;
- PF3 PR #95 — provider-neutral ExecutionNode + deterministic deployment/health/rollback foundation, exact candidate `4a3e0b342ec06c936693c8f583ed4f7a4fdc2007`, integrated after Core #671 + M12 #439 as merge `df84a72d6705aa78cb0c69df9e47a367098b74bb`.

## Product Factory dependency flow

### PF1 — durable ProductProject
PF1 #91 is **INTEGRATED**. PF5 consumes the public `ProductProjectRepository` create/get/update-spec and research-handoff contracts.

The integrated PF1 API still does not expose a durable product-decision approve/reject write operation. PF5 must not bypass this ownership boundary with direct SQL. Product decision persistence remains an explicit upstream capability gap rather than a false-complete journey claim.

### PF2 — orchestration
PRs #92/#93/#94/#97 are **INTEGRATED**. The integrated surface includes team/repository graph, coordinator state, CodingWorkerPort adaptation and restart recovery.

Open follow-up PR #98, `auto-pf2/product-project-binding`, head `14036a6d0d484afbd68fc36c2ce746e73c5d828c`, is **NOT INTEGRATED**. PF5 does not import or rely on it.

### PF3 — execution/deployment
PF3 #95 is **INTEGRATED**. PF5 may now consume its public execution-node, lease, exact release, staging, health, rollback and deployment snapshot contracts. PF5 does not execute provider deployments, expose provider credential references, or invent a Product Operations service that PF3 has not integrated.

### PF4 — acceptance gatekeeper
PF4 remains the independent PF0–PF12 acceptance/evidence lane. It rejects stale/mismatched SHA evidence and must not become a competing feature writer.

### PF5 — command journey/release owner
PF5 PR #90 is integrated. Current real PF5 code/evidence PR is #96, `auto-pf5/command-journey-pf2-presentation`.

PR #96 now advances one downstream Product Journey family against integrated upstream contracts:
- conservative deterministic Ukrainian + English ProductProject/Toolsmith routing;
- explicit ambiguity for mixed product/capability intent;
- integrated PF1 ProductProject create/inspect/update through the canonical durable repository;
- visible optimistic version checking, SQLite restart continuity and credential-reference redaction tests;
- integrated PF2 CoordinatorSnapshot/WorkRecord → textual component/review/QA/blocker presentation;
- integrated PF3 ExecutionRegistrySnapshot/DeploymentFabricSnapshot → textual node/build/release/staging/health/rollback/blocker presentation;
- PF3 `provider_ref` is intentionally absent from the PF5 presentation boundary;
- product-decision writes still fail closed until PF1 exposes a public durable decision-write API;
- canonical status reconciliation occurs in this same real code/evidence PR.

The previous #96 exact head `ee303c76da16adef5a4519ce9068839c73cd2c0e` passed Core #679 but M12 #447 failed and is superseded. Its history is preserved at `backup/auto-pf5-96-ee303c76`. The current candidate is being rebuilt linearly from live main `df84a72d6705aa78cb0c69df9e47a367098b74bb`; only fresh exact-head evidence on the final rebuilt SHA may receive merge credit.

## Shared/manual ownership

Scheduled Product Factory workers do not edit active manual DEV01–DEV05/M10 production slices. Current relevant owners include DEV01 #86, DEV02 #72, DEV03 #67, DEV04 #78, DEV05 #89 and M10 #61/#62.

DEV04 PR #78 retains Interaction/UIA/shared semantic UI ownership and its dedicated live Windows UIA proof remains blocked by duplicate semantic-node identity. PF5 does not edit shared DesktopBackend/web/UIA files.

## Accessibility and UI truth

The primary user remains Windows/NVDA-first. Automated semantic/UIA/WebView2 tests never set `NVDA_VERIFIED=true`.

PF5 currently exposes native/API and textual presentation contracts only. Interaction priority remains:
1. native/application API;
2. DOM/UIA/accessibility semantics;
3. named deterministic controls;
4. vision/OCR fallback;
5. coordinates last.

## Product Factory acceptance truth

Backend contracts are not Product Factory completion. PF11 still requires a representative request through the real factory: research, durable ProductProject, required product decision, acceptance criteria, dynamic team, repository, isolated implementation, independent QA/accessibility, package/release provenance, restart/resume and explicit human-only items.

The representative expense application is an acceptance scenario, not code hard-coded into Nika Core.

## Release/package truth

No Product Factory Windows candidate is promoted from PF5 #96. Package/release work starts only at a meaningful integrated exact-SHA milestone. The known packaged WebView2/UIA blocker is shared-UI ownership, not permission for PF5 to weaken or bypass accessibility gates.

## Next dependency-ordered wave

1. PF5 finishes #96 current-main rebuild, local/best-effort preflight and fresh exact-head Core/M12.
2. PF1 owner adds a durable public product-decision write boundary before PF5 can claim create/inspect/update/decision completeness.
3. PF2 repairs/integrates #98 independently; PF5 consumes it only after integration.
4. PF3 follow-up may add credential/real-provider/operations capability under its own ownership; PF5 consumes only integrated public contracts.
5. Shared semantic UI wiring waits for DEV04 ownership release plus an explicit compatibility decision.
6. PF11 packaging/release follows only after the representative integrated journey exists.

No invented Full Product Vision percentage is assigned. Progress is reported through exact executable acceptance states: IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.
