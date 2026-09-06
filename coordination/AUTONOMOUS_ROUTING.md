# Nika Core — current autonomous routing

EPOCH: EPOCH-0004
STATUS: ACTIVE
AUDIT_TIME: 2026-09-06T23:18:00+02:00
AUDIT_BASE_MAIN: 8cba6f5fba3f98bee0a575b41b7210bd80247d1c
LAST_GLOBAL_AUDIT: 2026-09-06T23:18:00+02:00
NEXT_AUDIT_RULE: first eligible coordinator-capable worker after >=6h, or immediately after a major integration/blocker/Windows-NVDA readiness change
FAILOVER_AUDIT_RULE: another capable worker may refresh after approximately 8h without a valid audit
CURRENT_VERSION: V0.1_ONLY_UNTIL_RELEASE

## Source-of-truth hierarchy

1. Live GitHub main, exact candidate heads, reviews and Actions are technical truth.
2. This file is the current project-local routing authority.
3. Issue #553 is the durable ownership/event stream and may carry an emergency routing checkpoint newer than this branch.
4. Google Drive mirrors routing in owner-readable form for cross-account continuity.
5. Static Scheduled Task prompts define stable home lanes and startup procedure only. They never carry exact PR/SHA priorities and never override this routing.

Every Scheduled Worker, Codex Cloud run and Work run must reread this hierarchy before substantive work.

## Owner-facing V0.1 truth

### Integrated and DO NOT REIMPLEMENT

The following foundations are already on main and are regression/composition dependencies only:

- real packaged canonical three-agent execution;
- durable keyboard source setup and task-source binding;
- one provider-neutral multi-agent ModelGateway runtime with practical local and configured API routes;
- canonical credential-reference API route boundary;
- startup/runtime recovery foundations;
- terminal task authority over scheduled wakes;
- durable pre-effect offline/reconnect wait and safe continuation;
- fail-closed PENDING/UNCERTAIN external-effect semantics: ambiguous effects are not blindly resent;
- browser stale-DOM/frame/document semantic authority;
- Windows autostart backend;
- canonical per-user data location, legacy-data adoption and recovery foundations.

These foundations do not mean the complete Windows release is finished.

### Still open before usable Windows/NVDA V0.1

1. Durable active Pause/Resume across process/Windows restart.
2. Current-main recurrence / hibernate / missed-run lifecycle.
3. One accepted current-main browser Scenario B:
   20 declared targets -> max 5 active -> bounded readiness -> task-owned tabs -> semantic action -> observable verification -> effect-safe retry/isolation -> durable cursor/wait -> bounded per-target report -> pause/restart/resume -> cancel.
4. One accepted current-main monitoring Scenario A using accepted recurrence and durable observation/change/condition/report components.
5. Packaged user provider/model selection -> durable safe config -> already-integrated ModelGatewayAgentRuntime -> same three-agent task after restart.
6. User-visible Windows autostart settings + current-main packaged restart proof.
7. Final Session/Worker Control Center truth for real task/team/model/offline/recovering/paused/uncertain states.
8. Final package/install/update/recovery/SBOM/provenance/governance qualification.
9. One exact integrated release candidate.
10. Oleksii human Windows keyboard/NVDA acceptance and repair loop.

HUMAN_TESTED=false
NVDA_VERIFIED=false
PRODUCTION_RELEASE_READY=false

## Current main / governance

- Audit main: 8cba6f5fba3f98bee0a575b41b7210bd80247d1c.
- Current-main Core CI is terminal SUCCESS.
- main is not protected and required-status enforcement is off. This remains a release-governance blocker.
- Future Living Agent/voice/mobile/self-learning/Product Factory implementation is preserved in architecture but frozen until V0.1 unless a concrete direct V0.1 dependency is proven.

## Classification of prior directions

- Offline/reconnect implementation: STOP_STALE. Keep only regression/composition coverage.
- Local/API ModelGateway core/provider router: STOP_STALE. PROMOTE packaged model/provider choice.
- Source setup: STOP_STALE. Keep as final product dependency.
- Browser stale-DOM/frame authority: STOP_STALE. Keep as Scenario-B dependency.
- Windows autostart backend: STOP_STALE. PROMOTE packaged setting/restart proof.
- Durable recurrence/hibernate: KEEP/PROMOTE; current-main repair is active.
- Durable Pause/Resume restart: PROMOTE after recurrence ownership permits.
- Durable batch cursor: PROMOTE; waiting independent same-head qualification.
- Task-owned tabs: KEEP but current candidate has a precise owner repair before acceptance.
- Bounded batch executor: KEEP/PROMOTE; current-main qualification active.
- Typed page readiness: KEEP/PROMOTE; current-main qualification active.
- Full Scenario B: PROMOTE after the above leaves qualify.
- Full monitoring Scenario A: PROMOTE after recurrence qualifies.
- QA_ONLY branches: NEVER_MERGE; preserve unique evidence only.
- Historical superseded production branches: STOP_STALE as merge targets after unique fixes/evidence are mapped.

## Independent-audit fail-safe

The five-worker topology must never deadlock because Integrator cannot self-audit.

Worker 1 has a secondary independent-auditor duty for OTHER lanes:

1. before beginning a new Stability source mutation, inspect the routing audit queue;
2. if an exact-head production candidate from another lane is CI-green and waiting only independent same-head review, audit it first;
3. Worker 1 must NEVER independently clear a Stability candidate authored/owned by Worker 1;
4. after external audit queue is consumed, Worker 1 returns to Stability development;
5. Worker 5 remains merge-only and never self-audits.

Current external audit priority:
- durable batch cursor first when exact head remains unchanged and green;
- Windows autostart/settings next after its current exact head finishes required gates;
- then other cross-lane exact-green leaves in routing order.

## Worker 1 — Stability / continuity + external audit fail-safe

HOME_LANE:
long tasks; pause/resume; restart; recurrence; hibernate/misfire; offline/reconnect regression; no duplicate effects.

STARTUP:
read this routing file, newest Issue #553 routing checkpoint, live main, open ownership and exact Actions; classify the previous target KEEP / CHANGE / STOP_STALE / COLLISION / PROMOTE.

CURRENT DEVELOPMENT TARGET:
- finish the incumbent recurrence current-main repair with only the already-localized compatibility/lint corrections; preserve canonical scheduler/cancel/effect authorities;
- after recurrence is independently accepted/integrated or ownership blocks further work, take durable active Pause/Resume across restart from the incumbent lineage;
- do not rebuild offline/reconnect.

CURRENT AUDIT DUTY:
- consume exact-green external audit leaves before new Stability mutation;
- never self-audit recurrence/pause work owned by this worker.

If current work is waiting CI/owner, perform a useful independent audit or take the next unowned Stability package instead of idling.

## Worker 2 — AI / ModelGateway product composition

HOME_LANE:
local AI; API AI; ModelGateway; user provider/model configuration; future 12-6-compatible brain port.

DO_NOT_REPEAT:
- local/API provider router;
- ModelGateway core;
- new local/API runtime adapter.

CURRENT TARGET:
- implement/qualify the final packaged provider/model selection seam;
- user choice must be durable, safe and restart-stable;
- the choice must drive the already-integrated ModelGatewayAgentRuntime used by the real three-agent task;
- no silent local/cloud switching;
- no raw credentials in task state, handoffs, audit or UI;
- no silent model download;
- bounded timeout/cancel and safe error projection.

12-6 remains a future replaceable brain/provider boundary only. No Living Agent/voice/self-learning source expansion before V0.1.

If UI files are owned by Worker 4, stay on backend/config/runtime seam and publish compatibility handoff rather than colliding.

## Worker 3 — Actions / browser / monitoring

HOME_LANE:
browser; bounded batches; task-owned tabs; readiness; durable cursor; per-target report; monitoring; long automation.

CURRENT LIVE FRONT:
- durable batch cursor is green but still requires independent same-head audit before integration;
- task-owned tabs candidate has a real Work-found defect: ordinary ephemeral query/fragment navigation must remain allowed under NEVER policy while durable SAME_TARGET persistence must reject query/fragment material. Repair only this distinction; do not create a new tab manager;
- bounded max-five batch executor current-main qualification is active;
- typed page-readiness current-main qualification is active.

CURRENT TARGET ORDER:
1. repair the precise task-tab persistence-vs-ephemeral-navigation defect;
2. finish current qualification of bounded executor and typed readiness;
3. after cursor/tabs/readiness/batch leaves qualify, compose one exact Scenario B rather than accumulating sibling greens;
4. add existing observable verification, effect-safe retry/isolation, durable inter-batch wait and per-target report;
5. after recurrence acceptance, compose monitoring Scenario A from existing monitor components.

No second browser/scheduler/retry/monitor engine.

## Worker 4 — Windows / accessibility / package readiness

HOME_LANE:
Windows 11; keyboard-only; WebView2/UIA; autostart settings; Session/Worker Control Center; package/install QA.

CURRENT TARGET:
- finish current-main qualification of the existing packaged autostart settings/restart-proof lineage;
- do not create a second autostart backend;
- prove backend-acknowledged read/enable/disable/stale state, no blind retry, keyboard/focus semantics and packaged restart persistence;
- continue Session/Worker Control Center only against real durable backend state;
- expose real task/team/model/offline/recovering/paused/uncertain truth;
- prepare exact package/install/restart/UIA evidence.

Automation may prove keyboard/UIA mechanics but never HUMAN_TESTED or NVDA_VERIFIED.

## Worker 5 — Integrator / release / periodic coordinator

HOME_LANE:
guarded integration; release candidate; packaging/recovery/governance; ownership resolution; periodic whole-project audit.

NORMAL RUN:
- read latest four worker checkpoints and this routing;
- integrate only exact-current, dependency-ready, required-CI-green, independently cleared production candidates;
- one merge at a time;
- after every merge reread main, Actions and ownership, then rebuild the queue;
- never merge QA_ONLY;
- between merges perform real package/governance/integration work.

COORDINATION DUTY:
- if no valid audit exists, >=6h elapsed, or a major trigger occurs, perform full project audit and increment routing epoch;
- after audit immediately return to productive integration/release work;
- failover audit may be performed by another capable worker after ~8h.

CURRENT MERGE FRONT:
- cursor once independently same-head cleared and unchanged;
- recurrence only after current CI + independent audit by someone other than its author;
- autostart settings after current gates + independent audit;
- browser leaves only after current exact qualification and any routed repair.

Governance blocker:
main branch protection/required-status enforcement remains off and must be resolved before release readiness can be true.

## Current candidate state at EPOCH-0004

### Recurrence / hibernate
CLASSIFICATION: KEEP / ACTIVE_OWNER / WAITING_CI.
The incumbent was repaired and converged onto current main; fresh Core/M12 are running. If green, route to an independent auditor that is not Worker 1.

### Durable batch cursor
CLASSIFICATION: PROMOTE / WAITING_AUDIT.
Current exact head has Core + complete M12 green and is mergeable, but no independent same-head review is yet recorded. Worker 1 audit fail-safe owns the next review if unchanged.

### Task-owned browser tabs
CLASSIFICATION: REAL_DEFECT / ACTIVE_OWNER.
Current-main candidate exists, but Work found an over-broad query/fragment rejection. Keep durable SAME_TARGET secret-safe, while allowing ordinary ephemeral query/fragment navigation under NEVER policy. Owner repairs and requalifies exact head.

### Bounded max-five batch execution
CLASSIFICATION: KEEP / WAITING_CI.
Current-main thin successor exists; fresh Core/M12 are running.

### Typed page readiness
CLASSIFICATION: KEEP / WAITING_CI.
Current-main thin successor exists; fresh Core/M12 are running.

### Windows autostart settings / packaged restart proof
CLASSIFICATION: PROMOTE / WAITING_CI_THEN_AUDIT.
Current-main candidate exists and M11 already passed; Core/M12 are running. If green, Worker 1 may independently audit it after cursor, provided Worker 1 did not author it.

### Packaged model/provider selection
CLASSIFICATION: PROMOTE / UNLEASED_OR_OWNER_CHECK_REQUIRED.
ModelGateway/local/API runtime is already integrated. The remaining work is durable user selection -> same runtime -> restart-stable product path. Worker 2 owns discovery/claim of the thin unowned seam.

## Codex Cloud

CURRENT_ACTIVE_PACKAGE: NONE CONFIRMED BY THIS AUDIT.

NEXT_RECOMMENDED_PACKAGE:
Repository-wide stale/superseded V0.1 retirement and exact reuse/dependency map, disjoint from active Worker 1–5 source ownership:
- map open historical production/QA lineages;
- preserve unique QA evidence and unique unintegrated fixes;
- mark/close only truly superseded work;
- produce the compact current-main dependency/reuse graph;
- then take the highest-value explicitly unleased seam from this routing.

Codex checkpoints after every coherent phase so credit exhaustion is recoverable without Oleksii.

## Work / principal architect

RECENT_RESULT:
The latest Work-style audit found the concrete task-owned-tabs persistence-vs-ephemeral-navigation defect now routed to Worker 3. This is useful product evidence and changes current B04 acceptance.

NEXT_RECOMMENDED_PACKAGE:
Deep clean-package V0.1 cross-system audit, disjoint from Worker source leases:
package/source/model settings -> real three-agent task -> browser/monitor -> offline/restart/pause -> result/report -> data recovery.
Find the FIRST cross-system user-visible failure not already leased. Implement only that disjoint hard package if clearly unowned; otherwise route precisely and continue auditing.

## Parallel / integration order

Parallel fronts:
A. recurrence -> Pause/Resume continuity;
B. cursor audit + tabs repair + bounded batch + page readiness;
C. packaged model/provider selection;
D. current autostart settings/restart proof;
E. package/governance preparation that does not assume unintegrated features.

Convergence:
1. accepted browser leaves -> one exact Scenario B;
2. accepted recurrence -> one exact monitoring Scenario A;
3. packaged model selection + Windows control-center truth;
4. exact Windows release candidate;
5. clean install/update/recovery/SBOM/provenance/governance;
6. complete automated V0.1 acceptance;
7. Oleksii human Windows keyboard + NVDA test;
8. repair human-found defects;
9. V0.1 release.

## Audit cadence

LAST_GLOBAL_AUDIT: 2026-09-06T23:18:00+02:00
NEXT_FULL_AUDIT_NOT_BEFORE: 2026-09-07T05:18:00+02:00 unless a major trigger occurs
FAILOVER_AUDIT_AFTER: approximately 2026-09-07T07:18:00+02:00 if no valid refresh exists

Major trigger:
- major V0.1 merge;
- a blocker opens/closes;
- Windows/NVDA/package readiness changes;
- Work/Codex completes a major package;
- collision or stale ownership is found;
- routing contradicts live project truth.

## Short owner-readable Ukrainian summary

Nika V0.1 вже має реальну команду з трьох агентів, налаштування джерел, єдиний local/API AI шлях, безпечне відновлення після втрати мережі, захист від повторення невизначених зовнішніх дій, базовий автозапуск Windows, відновлення даних і значну частину безпечної браузерної взаємодії.

Головне, що ще не завершено: Pause/Resume після перезапуску, recurrence/hibernate, повний 20-target browser сценарій, повний monitoring сценарій, користувацький вибір моделі, фінальний autostart UI/restart proof, installer/package/governance та реальна перевірка Олексієм з NVDA.

Олексій не повинен вручну переписувати п'ять Scheduled Tasks або розподіляти звичайну технічну роботу. П'ять стабільних воркерів читають цей живий план, автоматично відкидають завершене/застаріле/зайняте і переходять до наступного незайнятого V0.1 пакета.
