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

`2145561509ce655adb89ed0a3e8aa027d0a7940d`

This main includes:
- PF5 PR #90 command/presentation routing foundation;
- PF2 PR #92 Dynamic Team Composer + ProductRepositoryGraph foundation;
- PF2 PR #93 deterministic Product Factory coordinator/reconciliation, integrated from exact candidate `5f84e4c3380b0834b1a9b0141fe7f3e1f3e23661` after Core CI #646 and M12 #414 succeeded.

The latest main merge for PR #93 explicitly preserves manual DEV ownership. `HUMAN_TESTED` and `NVDA_VERIFIED` remain false.

## Product Factory dependency flow

### PF1 — durable ProductProject
PR #91, branch `auto/pf1-product-project`, head `a973b82e096f642f82c2e9f53124484c5542a6f3`.

Implemented durable ProductProject + Research→Product foundation. Its exact head passed Core #640 and M12 #408, but `main` advanced afterward. State: **GREEN-BUT-STALE / NOT INTEGRATED**. PF1 must refresh compatibility against current main and obtain fresh exact-head gates before merge.

PF5 must not import PF1 code until it is integrated.

### PF2 — orchestration
PF2 PR #93 is **INTEGRATED** on current main.

Open PF2 follow-up PR #94, `auto-pf2/coding-worker-adapter`, head `925e13ef349cfa10e127abacefe3ba0e329a4f77`, adapts integrated Product Factory component work to the public CodingWorkerPort. It is open/draft and receives no PF5 integration credit until merged.

### PF3 — execution/deployment
Open PR #95, `auto-pf3/execution-deployment-foundation`, head `1322547198d6b847869ed9a53a7e7964616abc32`.

It is open/draft and not integrated. PF5 may not import its branch contracts until merge.

### PF4 — acceptance gatekeeper
PF4 remains the independent PF0–PF12 acceptance/evidence lane. It must reject stale or mismatched SHA evidence and must not become a competing feature writer.

### PF5 — command journey/release owner
PF5 PR #90 is integrated.

Current real PF5 batch is PR #96, `auto-pf5/command-journey-pf2-presentation`, based on exact main `2145561509ce655adb89ed0a3e8aa027d0a7940d`.

Its scope is:
- conservative deterministic Ukrainian + English ProductProject/Toolsmith command classification;
- explicit ambiguity when product and capability-building intents overlap;
- projection of the **integrated PF2** CoordinatorSnapshot/WorkRecord state into stable PF5 textual ProductStatusEntry contracts;
- textual component/repository/base-SHA/attempt/allowed-path/review/QA/blocker evidence suitable for later semantic UI consumption;
- focused command/presentation/accessibility-oriented tests;
- no PF1 persistence, PF2 #94, PF3 #95 or shared UI branch imports.

PR #96 is not integrated until its exact-head required gates are green and current-main compatibility is rechecked.

## Shared/manual ownership

Scheduled Product Factory workers do not edit active manual DEV01–DEV05/M10 production slices.

Current relevant open manual owners include:
- DEV01 PR #86 — Research/Corpus exports;
- DEV02 PR #72 — Windows worker containment proof;
- DEV03 PR #67 — deterministic trader replay/accounting/risk;
- DEV04 PR #78 — strict Windows UIA semantic vertical; shared interaction/UIA ownership remains with DEV04 and its dedicated live proof is not green;
- DEV05 PR #89 — stable subtitle acquisition;
- M10 PR #61 and stacked R4 PR #62 — security-sensitive authorization/approval ownership.

PF5 does not edit their owned source without an explicit compatibility decision.

## Accessibility and UI truth

The primary user remains Windows/NVDA-first. Automated semantic/UIA/WebView2 tests never set `NVDA_VERIFIED=true`.

Shared Windows semantic UI is not PF5-owned while DEV04/shared UI ownership is active. PF5 currently advances framework-neutral textual presentation and command contracts only. Interaction priority remains:
1. native/application API;
2. DOM/UIA/accessibility semantics;
3. named deterministic controls;
4. vision/OCR fallback;
5. coordinates last.

## Product Factory acceptance truth

Backend contracts are not Product Factory completion. PF11 still requires a representative product request from a clean packaged Windows Nika installation with research, durable ProductProject, product decision, acceptance criteria, dynamic team, repository connection/creation, isolated implementation, independent QA/accessibility evidence, build/package, release provenance, restart/resume and explicit human-only items.

No representative expense application is hard-coded into Core; it must eventually be created by the real factory.

## Release/package truth

Do not build/promote a Product Factory Windows package merely because an isolated backend PR is green. Package/release work starts only at a meaningful integrated exact-SHA milestone. Any integrated behavior change supersedes older human-candidate artifacts until a fresh combined release gate succeeds.

## Next dependency-ordered wave

1. PF1 refreshes #91 against current main and integrates only after fresh exact-head evidence.
2. PF2 completes #94 independently; PF5 consumes it only after integration.
3. PF3 completes #95 independently; PF5 consumes it only after integration.
4. PF5 drives PR #96 through exact-head CI and integrates only if current-main compatible.
5. After PF1 integration, PF5 adds the real ProductProject create/inspect/update/decision adapter and restart-aware Product Journey tests.
6. After PF2/PF3 follow-ups integrate, PF5 exposes team/repository/component/work/review/repair and node/build/staging/health/rollback/ops through the same stable presentation layer without credential material.
7. Shared semantic UI wiring waits for ownership to be free and an explicit compatibility decision.
8. PF11 packaging/release is attempted only after the representative end-to-end factory journey exists on integrated contracts.

No invented Full Product Vision percentage is assigned. Progress is reported through exact executable acceptance states: IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.
