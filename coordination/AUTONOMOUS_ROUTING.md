# Nika Core — current autonomous routing

EPOCH: EPOCH-0003
STATUS: ACTIVE
AUDIT_TIME: 2026-09-06T22:20:00+02:00
AUDIT_BASE_MAIN: 8cba6f5fba3f98bee0a575b41b7210bd80247d1c
LAST_GLOBAL_AUDIT: 2026-09-06T22:20:00+02:00
NEXT_AUDIT_RULE: first eligible coordinator-capable worker after >=6h, or immediately after a major integration/blocker/Windows-NVDA readiness change
FAILOVER_AUDIT_RULE: another capable worker may refresh after approximately 8h without a valid audit
CURRENT_VERSION: V0.1_ONLY_UNTIL_RELEASE

## Source-of-truth hierarchy

1. Live GitHub `main`, exact candidate heads, reviews and Actions are technical truth.
2. This file is the current project-local routing authority.
3. Issue #553 is the durable coordination/ownership/event stream and may carry a newer emergency routing checkpoint while a routing PR is pending.
4. Google Drive mirrors this routing in owner-readable form for cross-account continuity.
5. Static Scheduled Task prompts define only stable home lanes and startup procedure. They never override newer live routing.

Every recurring worker, Codex Cloud run and Work run must reread this hierarchy before substantive work.

## Current owner-facing V0.1 truth

### Already integrated and must not be reimplemented

- real packaged three-agent execution path;
- packaged user source setup for the representative V0.1 journey;
- provider-neutral ModelGateway composition with practical local and configured API routes;
- startup/runtime recovery foundations;
- durable pre-effect offline/reconnect wait and safe continuation;
- fail-closed PENDING/UNCERTAIN external-effect semantics: ambiguous effects are not blindly resent;
- semantic browser stale-DOM/frame/document identity hardening;
- Windows autostart backend;
- canonical per-user data/recovery/migration foundations.

These are integrated foundations, not proof that the complete final Windows product is released.

### Still open before usable Windows/NVDA V0.1

- active durable Pause/Resume across process/Windows restart;
- current-main recurrence / hibernate / missed-run lifecycle;
- one accepted current-main browser Scenario B: 20 declared targets, max 5 active, task-owned tabs, semantic action, observable verification, effect-safe retry/isolation, durable cursor/wait and bounded per-target report;
- one accepted current-main monitoring Scenario A: durable previous/current observation, change detection, condition/deadline, recurrence and report;
- packaged model-selection/configuration path: saved user choice must select the already-integrated local/API ModelGateway runtime and survive restart without leaking credentials;
- user-visible Windows autostart setting plus real packaged restart/persistence proof on the current integrated product;
- final clean-install/package/update/recovery/SBOM/provenance/governance qualification;
- exact integrated V0.1 release candidate;
- human Windows keyboard/NVDA acceptance and repair loop.

HUMAN_TESTED=false
NVDA_VERIFIED=false
PRODUCTION_RELEASE_READY=false

## Current product evidence

- Current audited main at this epoch: `8cba6f5fba3f98bee0a575b41b7210bd80247d1c`.
- Exact current-main Core CI is terminal SUCCESS.
- Main branch protection / required-status enforcement is not proven and remains a release-governance blocker.
- Current exact-main Core green alone is not final package/release evidence.

## Do not repeat / stale or superseded work

- STOP_STALE: new offline/reconnect implementation. The current foundation is already integrated; only regression/composition work remains.
- STOP_STALE: new local/API ModelGateway core/provider router. The unified runtime path is already integrated.
- STOP_STALE: new source-setup implementation. Source setup is integrated.
- STOP_STALE: new stale-DOM/frame semantic browser authority implementation. It is integrated.
- STOP_STALE: a second Windows autostart backend. The backend is integrated.
- STOP_STALE as merge targets: historical cursor branches superseded by the newest exact-current successor process. Preserve unique evidence only.
- STOP_STALE as merge target: historical connectivity candidate superseded by the integrated successor.
- QA_ONLY: evidence/oracles only; NEVER_MERGE.
- FROZEN_FUTURE_SCOPE: Living Agent voice, Telegram/mobile embodiment, camera, self-learning source expansion, Product Factory and other V0.2+ work unless a concrete direct V0.1 dependency is proven.
- Historical green/audit on an old SHA is evidence only and never transfers to a moved candidate.

## Old-direction classification

- Offline/reconnect: STOP_STALE as implementation; KEEP as integrated regression dependency.
- Local/API ModelGateway core: STOP_STALE as implementation; PROMOTE the final packaged model-choice seam.
- Durable Pause/Resume: PROMOTE.
- Recurrence/hibernate/misfire: PROMOTE.
- Browser durable cursor: CHANGE to the newest current-main successor; older cursor candidates are STOP_STALE/SUPERSEDED.
- Full browser Scenario B composition: PROMOTE.
- Monitoring component lineages: CHANGE from isolated components to one current-main composition; do not create a second monitoring/scheduler framework.
- Windows autostart backend: STOP_STALE as backend implementation; PROMOTE accessible UI/settings + packaged restart proof.
- Final package/install/recovery/governance: PROMOTE.
- Living Agent / voice / 12-6 learning loops: KEEP as official future architecture, FROZEN for V0.1 production work.

## Worker 1 — Stability / continuity

HOME_LANE: runtime/task durability, pause/resume, restart, offline/reconnect, recurrence, no duplicate actions.

CURRENT_TARGET:
1. Repair and converge current-main durable recurrence/hibernate candidate. The currently known candidate is semantically narrow but has a small Ruff/lint RED and stale base; fix only exact lint defects, converge current main, preserve canonical scheduler/cancel/effect authority, rerun focused/Core/M12 and request independent audit.
2. PROMOTE durable active Pause/Resume across restart. Reuse the incumbent historical pause lineage; do not fork a second runtime coordinator. If the incumbent has not moved, produce a thin current-main successor only after ownership verification.
3. Keep integrated offline/reconnect and uncertain-effect behavior as regression/composition dependencies, not new implementations.

AVOID:
- second scheduler;
- second effect ledger;
- reimplementing integrated reconnect;
- touching browser/UI/model ownership without an explicit dependency handoff.

## Worker 2 — AI / ModelGateway

HOME_LANE: local AI, API AI, ModelGateway, model configuration and future 12-6-compatible port boundary.

CURRENT_TARGET:
1. Do NOT rebuild local/API routes; they are integrated.
2. PROMOTE the final packaged model-selection seam: user-configured provider/model -> durable safe settings -> same integrated ModelGatewayAgentRuntime -> real three-agent product task.
3. Prove restart-stable provider/model identity, bounded timeout/cancel, safe credential reference/config boundary, no raw secret in task/handoff/audit/UI, no silent local<->cloud switching and no silent model download.
4. Keep 12-6 as a future replaceable brain/provider boundary only. Do not start 12-6/Living Agent implementation under V0.1.

If another active owner already owns the packaged setting/UI source, implement only backend/config/runtime seam or provide a compatibility handoff rather than colliding.

## Worker 3 — Actions / browser / monitoring

HOME_LANE: semantic browser execution, bounded batches, monitoring and long automation.

CURRENT_TARGET:
1. Take the newest durable batch-cursor successor and keep it exact-current. A predecessor already passed Core/M12 + independent audit but became stale only because main moved through coordination/docs commits; REUSE the audited two-file semantic delta, do not rewrite the cursor engine.
2. After cursor integration, compose one current-main Scenario B:
   declared 20 targets -> max 5 active -> task-owned tabs -> governed navigation -> semantic action -> observable verification -> effect-safe retry/isolation -> durable inter-batch wait/cursor -> pause/restart/resume -> bounded per-target report -> cancel stops future work.
3. Then compose one current-main monitoring Scenario A using existing observation/change/condition/report components plus accepted recurrence. No second scheduler/monitor engine.

CURRENT_FIRST_PRODUCT_BREAK: no accepted one-head Scenario B yet.

## Worker 4 — Windows / user readiness

HOME_LANE: Windows 11, accessibility, keyboard-only, packaged UX, autostart settings, package/install QA.

CURRENT_TARGET:
1. Converge the existing packaged autostart-settings/restart-proof candidate onto current main. Its old exact head had Core/M11/M12 success; that evidence cannot transfer, but the implementation should be reused rather than rewritten.
2. Prove user-visible checkbox/settings truth against the integrated autostart backend and actual packaged restart persistence.
3. Continue final Session/Worker Control Center product truth: real task/team/model/offline/recovering/paused/uncertain states; Start/Pause/Resume/Cancel backed by durable state; no JS-owned lifecycle.
4. Prepare and automate keyboard/focus/UIA/package checks on the exact composed candidate.
5. Prepare the human NVDA protocol, but never set HUMAN_TESTED/NVDA_VERIFIED from automation.

## Worker 5 — Integration / release / periodic coordinator

HOME_LANE: guarded integration, exact release candidate, packaging/recovery/governance, ownership and periodic whole-project audit.

NORMAL_RUN:
- integrate only exact-current, dependency-ready, same-head required-CI-green, independently cleared production candidates one at a time;
- after each merge reread main, rebuild dependencies and invalidate stale evidence;
- continue real package/recovery/governance work rather than acting as a permanent passive coordinator.

COORDINATION_DUTY:
- if no valid audit exists, >=6h elapsed, or a major trigger occurred, perform full audit and update this routing epoch;
- after audit immediately resume productive integration/release work;
- if no audit by ~8h, another capable worker may perform failover refresh.

CURRENT_INTEGRATION_FRONTIER:
- no stale predecessor should be merged merely because it was green;
- consume the first requalified current-main leaf among recurrence/pause, cursor/Scenario-B, packaged model selection and autostart UI;
- branch protection/required status enforcement remains a final governance blocker.

## Current candidate routing

### Durable batch cursor
CLASSIFICATION: CHANGE / PROMOTE.
A recent exact head passed Core + complete M12 and independent review, but main advanced through coordination/docs changes. Create/reuse only a thin exact-current successor of the already-audited delta. Do not restart design.

### Durable recurrence
CLASSIFICATION: REAL_DEFECT + BASE_STALE.
The current known candidate fails on a small Ruff-only set before functional tests and is also based on an older main. Repair the narrow lint defects, converge to current main, then exact requalification. No scheduler redesign.

### Packaged autostart settings
CLASSIFICATION: CHANGE / BASE_STALE.
The known exact head passed Core + M11 + M12, but current main moved. Reuse implementation, converge, re-run current exact gates and independent review. Backend autostart itself is already integrated.

## Codex Cloud

CURRENT_ACTIVE_PACKAGE: NONE CONFIRMED BY THIS AUDIT.

NEXT_RECOMMENDED_PACKAGE:
Perform a repository-wide stale/superseded V0.1 PR retirement and exact reuse/dependency sweep without duplicating Workers 1–5:
- preserve unique QA evidence/oracle references;
- mark historical production predecessors as superseded when a current successor/integrated implementation exists;
- identify branches with any unique unintegrated production fix before closure;
- close/archive only when evidence is preserved and no unique production fix remains;
- leave a compact current-main candidate graph.
After cleanup, take the highest-value explicitly unleased integration seam from the latest routing epoch.

Codex must checkpoint after every coherent phase so credit exhaustion is recoverable.

## Work / principal architect

CURRENT_ACTIVE_PACKAGE: NONE CONFIRMED BY THIS AUDIT.

NEXT_RECOMMENDED_PACKAGE:
Deep V0.1 end-to-end product-path audit:
clean Windows package -> source/model settings -> three-agent task -> browser/monitor work -> offline/restart/pause -> result/report -> package data/recovery.
Find the first cross-system failure not already leased by Workers 1–5. Implement only that disjoint hard package or route it precisely. Refresh routing immediately after any major result.

## Parallel and integration order

Safe parallel fronts, subject to ownership:
A. Pause/recurrence continuity.
B. Batch cursor -> Scenario B.
C. Packaged model-selection seam.
D. Autostart UI/package restart proof.

Then converge:
1. full current-main Scenario B;
2. full current-main monitoring Scenario A;
3. exact Windows release candidate;
4. clean install/update/recovery/SBOM/provenance/governance;
5. full automated V0.1 acceptance;
6. Oleksii human Windows + NVDA test;
7. repair any human-found defects;
8. V0.1 release.

## Ownership / collision invariant

Before any production mutation:
- reread this file, Issue #553, open PRs/branches and recent claims;
- one writer per semantic source slice;
- active incumbent owner wins;
- shared-contract edit requires a compatibility decision;
- if collision exists, audit/route/choose another independent package instead of forking.

## Audit cadence

LAST_GLOBAL_AUDIT: 2026-09-06T22:20:00+02:00
NEXT_FULL_AUDIT_NOT_BEFORE: 2026-09-07T04:20:00+02:00 unless a major trigger occurs
FAILOVER_AUDIT_AFTER: approximately 2026-09-07T06:20:00+02:00 if no valid refresh exists

Major trigger:
- major V0.1 merge;
- blocker closes/appears;
- Windows/NVDA/package readiness changes;
- Work/Codex major package completes;
- collision/stale ownership discovered;
- routing contradicts live project truth.

## Short owner-readable summary

Nika V0.1 has moved materially beyond a prototype: real packaged three-agent execution, source setup, local/API AI through one gateway, safe pre-effect offline recovery, semantic browser safety and Windows autostart backend are already integrated. The release is still not ready for daily NVDA use. The shortest path now is to finish durable Pause/recurrence, one complete browser Scenario B, one complete monitoring Scenario A, packaged model/autostart settings, then build and qualify one exact Windows release candidate and run the human NVDA test.

Oleksii should not need to manually rewrite worker prompts or choose routine next tasks. The workers, Codex and Work are required to read this routing state, compare it with live evidence, drop stale assignments and continue the highest-value unowned V0.1 work automatically.
