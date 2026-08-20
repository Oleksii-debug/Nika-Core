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

`be7b4ab4abd59a3e373ad48562e023b07febc98c`

Integrated Product Factory foundation includes:
- PF5 PR #90 — command/presentation routing foundation;
- PF1 PR #91 — durable ProductProject + Research→Product foundation;
- PF2 PR #92 — Dynamic Team Composer + ProductRepositoryGraph;
- PF2 PR #93 — deterministic Product Factory coordinator/reconciliation;
- PF2 PR #94 — public CodingWorkerPort adapter;
- PF2 PR #97 — restart recovery for in-flight component work;
- PF3 PR #95 — provider-neutral ExecutionNode + deterministic deployment/health/rollback foundation;
- PF5 PR #96 — ProductProject command journey plus PF2/PF3 textual execution/deployment presentation, exact head `5beb29c70ee0e5e72f729ad555a49384b3308c9c`, integrated after Core #681 + M12 #449 as merge `be7b4ab4abd59a3e373ad48562e023b07febc98c`.

## Product Factory dependency flow

### PF1 — durable ProductProject
PF1 #91 is **INTEGRATED**. PF5 consumes the public `ProductProjectRepository` create/get/update-spec and research-handoff contracts.

The integrated PF1 API still does not expose a durable product-decision approve/reject write operation. PF5 must not bypass this ownership boundary with direct SQL. Product decision persistence remains an explicit upstream capability gap rather than a false-complete journey claim.

### PF2 — orchestration
PRs #92/#93/#94/#97 are **INTEGRATED**.

Open follow-up PR #98, `auto-pf2/product-project-binding`, exact head `bba309b4713d385e6b7a653fffbdec313866d499`, is based on current main and remains **NOT INTEGRATED**. It binds durable ProductProject version identity to coordinator checkpoints. PF5 does not import or rely on it until exact-head gates and integration complete.

### PF3 — execution/deployment/credentials
PF3 #95 is **INTEGRATED**. PF5 consumes only its public execution-node, lease, exact release, staging, health, rollback and deployment snapshot contracts.

Open PF3 follow-up #99, `auto-pf3/credential-broker-foundation`, exact head `3d68f5cb06b814c2917a608820226ca56628ffb7`, remains **NOT INTEGRATED**. PF5 does not import credential-broker internals, raw credential material or unmerged sibling contracts.

### PF4 — acceptance gatekeeper
PF4 remains the independent PF0–PF12 acceptance/evidence lane. It rejects stale/mismatched SHA evidence and must not become a competing feature writer.

### PF5 — command journey/release owner
PF5 #90 and #96 are **INTEGRATED**.

Current real PF5 code/evidence PR is #100, `auto-pf5/project-scoped-command-center`, starting from exact main `be7b4ab4abd59a3e373ad48562e023b07febc98c`.

PR #100 closes one downstream presentation-integrity family:
- composes integrated PF1/PF2/PF3 presentation only through a project-scoped `ProductCommandCenter`;
- requires PF2 `CoordinatorSnapshot` identity and every work record to match the inspected ProductProject;
- filters global PF3 execution snapshots to target-project leases and only the nodes serving those leases;
- filters PF3 deployment snapshots to target-project records/environment state;
- recomputes blocker count after composition;
- duplicate `(status kind, item_id)` presentation identity fails closed;
- existing PF3 `provider_ref`/credential non-disclosure remains intact;
- focused tests attack foreign/corrupt coordinator state, cross-project execution/deployment leakage and duplicate presentation identity.

The initial #100 exact head `13d9c1d62801969469f6e4e9e25cd37bb414d8f6` reached exact-checkout Core #687/M12 #455 before this required canonical status reconciliation. It is therefore lineage evidence only. Fresh exact-head gates are required on the final #100 SHA.

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

No Product Factory Windows candidate is promoted from PF5 #100. Package/release work starts only at a meaningful integrated exact-SHA milestone. The known packaged WebView2/UIA blocker remains shared-UI ownership, not permission for PF5 to weaken or bypass accessibility gates.

## Next dependency-ordered wave

1. PF5 proves #100 on its final exact head with fresh Core + M12, then rechecks live-main compatibility before merge.
2. PF1 owner adds a durable public product-decision write boundary before PF5 can claim create/inspect/update/decision completeness.
3. PF2 proves/integrates #98 independently; PF5 consumes it only after integration.
4. PF3 proves/integrates #99 independently; PF5 consumes only integrated public credential/identity contracts.
5. Shared semantic UI wiring waits for DEV04 ownership release plus an explicit compatibility decision.
6. PF11 packaging/release follows only after the representative integrated journey exists.

No invented Full Product Vision percentage is assigned. Progress is reported through exact executable acceptance states: IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.
