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

`700394d678bce7226374ee563e8e38ab76ab4538`

Integrated Product Factory foundation now includes:
- PF5 PR #90 command/presentation routing foundation;
- PF2 PR #92 Dynamic Team Composer + ProductRepositoryGraph foundation;
- PF2 PR #93 deterministic Product Factory coordinator/reconciliation;
- PF2 PR #94 public CodingWorkerPort adapter, merged from exact candidate `925e13ef349cfa10e127abacefe3ba0e329a4f77` after Core CI #653 and M12 #421 succeeded.

The latest main merge explicitly preserves manual DEV ownership. `HUMAN_TESTED` and `NVDA_VERIFIED` remain false.

## Product Factory dependency flow

### PF1 — durable ProductProject
PR #91, branch `auto/pf1-product-project`, current head `7ed579fcb5b269c95a82b6b818bd462dcda9fb39`.

PF1 remains **NOT INTEGRATED**. On this current head Core CI #660 succeeded but M12 #428 failed. Older green evidence on previous PF1 heads is stale and does not override the current exact-head failure. PF5 must not import PF1 code until PF1 repairs the failure family, refreshes current-main compatibility and integrates an exact green candidate.

### PF2 — orchestration
PF2 PR #93 and follow-up PR #94 are **INTEGRATED** on current main.

The integrated surface now includes team/repository graph and deterministic coordinator state plus the thin adapter from Product Factory component work to the public CodingWorkerPort. PF5 consumes only these integrated contracts and does not edit DEV02 worker/security internals.

### PF3 — execution/deployment
Open PR #95, branch `auto-pf3/execution-deployment-foundation`, current head `1322547198d6b847869ed9a53a7e7964616abc32`.

Core CI #654 and M12 #422 succeeded on that exact head, but the branch is still open/draft and based on an older main. State: **GREEN-BUT-UNINTEGRATED / CURRENT-MAIN COMPATIBILITY REQUIRED**. PF5 may not import its contracts until merge.

### PF4 — acceptance gatekeeper
PF4 remains the independent PF0–PF12 acceptance/evidence lane. It must reject stale or mismatched SHA evidence and must not become a competing feature writer.

### PF5 — command journey/release owner
PF5 PR #90 is integrated.

Current real PF5 batch is PR #96, `auto-pf5/command-journey-pf2-presentation`. The previous exact candidate `a7e8fc9d1e05cbc398e9b5c59a557ae7939daa04` passed Core CI #659 and M12 #427. Live main then advanced by six commits through integrated PF2 #94. Compare from PR #96 starting main showed those main-only commits changed only `src/nika_core/product_factory_coding_worker_adapter.py` and its focused test, with no overlap with PR #96's six owned files.

This same PR now refreshes canonical status to current main `700394d678bce7226374ee563e8e38ab76ab4538`; therefore the old green candidate is superseded and fresh exact-head Core + M12 are required before merge.

PF5 scope remains:
- conservative deterministic Ukrainian + English ProductProject/Toolsmith command classification;
- explicit ambiguity when product and capability-building intents overlap;
- projection of integrated PF2 CoordinatorSnapshot/WorkRecord state into stable PF5 textual ProductStatusEntry contracts;
- component/repository/base-SHA/attempt/allowed-path/review/QA/blocker evidence suitable for later semantic UI consumption;
- focused command/presentation/accessibility-oriented tests;
- no PF1 persistence, PF3 branch import or shared UI write.

## Shared/manual ownership

Scheduled Product Factory workers do not edit active manual DEV01–DEV05/M10 production slices.

Current relevant open manual owners include:
- DEV01 PR #86 — Research/Corpus exports;
- DEV02 PR #72 — Windows worker containment proof;
- DEV03 PR #67 — deterministic trader replay/accounting/risk;
- DEV04 PR #78 — strict Windows UIA semantic vertical; shared Interaction/UIA ownership remains with DEV04 and its dedicated live proof is not green;
- DEV05 PR #89 — stable subtitle acquisition;
- M10 PR #61 and stacked R4 PR #62 — security-sensitive authorization/approval ownership.

PF5 does not edit their owned source without an explicit compatibility decision.

## Accessibility and UI truth

The primary user remains Windows/NVDA-first. Automated semantic/UIA/WebView2 tests never set `NVDA_VERIFIED=true`.

Shared Windows semantic UI is not PF5-owned while DEV04/shared UI ownership is active. PF5 advances framework-neutral textual presentation and command contracts only. Interaction priority remains:
1. native/application API;
2. DOM/UIA/accessibility semantics;
3. named deterministic controls;
4. vision/OCR fallback;
5. coordinates last.

## Product Factory acceptance truth

Backend contracts are not Product Factory completion. PF11 still requires a representative product request from a clean packaged Windows Nika installation with research, durable ProductProject, product decision, acceptance criteria, dynamic team, repository connection/creation, isolated implementation, independent QA/accessibility evidence, build/package, release provenance, restart/resume and explicit human-only items.

The representative expense application is an acceptance scenario, not code to hard-code into Nika Core.

## Release/package truth

Do not build/promote a Product Factory Windows package merely because an isolated backend PR is green. Package/release work starts only at a meaningful integrated exact-SHA milestone. Any integrated behavior change supersedes older human-candidate artifacts until a fresh combined release gate succeeds.

## Next dependency-ordered wave

1. PF1 repairs current exact-head M12 failure on #91, refreshes compatibility and integrates only after fresh exact-head green evidence.
2. PF3 refreshes #95 against current main and integrates only after current-main compatibility + exact required evidence.
3. PF5 reruns fresh exact-head Core + M12 for #96 after this current-main/status reconciliation and merges only if still compatible.
4. After PF1 integration, PF5 adds the real ProductProject create/inspect/update/decision adapter and restart-aware Product Journey tests.
5. After PF3 integration, PF5 exposes node/build/staging/health/rollback/ops through the same stable presentation layer without credential material.
6. Shared semantic UI wiring waits for ownership to be free and an explicit compatibility decision.
7. PF11 packaging/release is attempted only after the representative end-to-end factory journey exists on integrated contracts.

No invented Full Product Vision percentage is assigned. Progress is reported through exact executable acceptance states: IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.
