# Nika Core — current autonomous routing

## LIVE REFRESH — EPOCH-CONTINUOUS-0003 — 2026-09-07T20:44+02:00
CURRENT_MAIN: `c0ed340c8ee6ddfab011f861f5e6f49017ff5286` (guarded merge #662 release archive/manifest/secret-safety).
STRATEGY: ONE_CONTINUOUS_FULL_PRODUCT; V0.1 is the nearest acceptance checkpoint, not a separate product.
LIVE_OVERRIDES_OLDER_SNAPSHOT: true.
BRANCH_PROTECTION: disabled; required-status enforcement off; this remains a binding release blocker.

### DONE / DO_NOT_REPEAT
- Integrated: #641 durable batch cursor, #656 checker handoffs, #659 Windows autostart/settings, #651 durable model-selection backend, #662 release archive/manifest/secret-safety.
- Do not rebuild canonical ModelGateway, scheduler, recovery authority, browser engine, cursor, effect ledger, desktop shell or package manifest verifier.

### IN_PROGRESS / BLOCKED / NEXT
- Worker1 #654 @ `636b13523f604825f5c7b362bee812a5e114689c`: exact-current but BLOCKED by confirmed PAUSED -> explicit Cancel race; independent Work diagnosis is authoritative. Repair incumbent coordinator only, replay unchanged #669, then fresh Core/M12 + non-self audit.
- Worker1 #638 @ `aacfa252e3c43a5e48ffceda54abb64755f29880`: exact-current, mergeable, Core 34154413451 SUCCESS + M12 34154413449 SUCCESS. Independent unchanged #664 recurrence-concurrency replay is actively claimed on QA branch; no merge until PASS_CURRENT.
- Worker3 #677 @ `80a34f22d48a79c909ec73e7016825b0ec2f784e`: exact-current Scenario-B composition; latest restart-wait repair is in fresh qualification; no acceptance credit until terminal gates + independent audit.
- Worker4 #672: shared packaged UIA RuntimeId resilience remains the Windows package dependency; head is moving under owner repair, so freeze downstream package/UI integration evidence until an exact terminal-green audited head exists.
- Worker4 #663: accessible packaged provider/model selector remains incumbent UI lineage; exact current head may move while compatibility repairs run.
- Worker5 #676 @ `4068ca1a1011fdff10d332eb6770ea4532b2d3f2`: installer/update/rollback security blockers repaired (reparse ancestor authority + post-activation rollback); fresh Core/M12 required, then non-self audit.
- Worker5 #665 @ `e80c9c8118c11ec7177c912cba119c0adc239e48`: exact-current governance evaluator; missing ruleset bypass_actors visibility now fails closed; fresh Core/M12 + non-self audit required. Evaluator does not enable protection.
- #660 legacy-data package proof is FROZEN_STALE until shared #672 UIA gate integrates; own M11 proof is good but old M12 failed only shared UIA.
- Worker2 #674 AI/model routing continues in owner lane; current integration credit is blocked until exact current-main compatibility and fresh gates are re-established.

### RELEASE / HUMAN TRUTH
CURRENT_V01_REMAINING_GATES: durable Pause/Resume/restart/cancel; recurrence/hibernate; exact Scenario B; exact monitoring Scenario A; packaged model UI/runtime composition; startup recovery; legacy-data/install/update/rollback/SBOM/notices/provenance; live governance; one exact Windows candidate; human Windows/NVDA protocol.
HUMAN_TESTED=false.
NVDA_VERIFIED=false.
PRODUCTION_RELEASE_READY=false.

### NEXT ROUTING
1. Consume independent #638 recurrence replay. PASS -> final exact reread and one guarded merge; BLOCK -> route exact defect to Worker1.
2. Consume fresh #676/#665 qualification while #638 audit runs; never self-audit Worker5-authored candidates.
3. Consume #672 exact terminal Windows package evidence before reconverging UI/package dependents.
4. Keep #654 as earliest runtime blocker and #677 as browser composition blocker; do not duplicate owners.
5. After every merge reread main and invalidate stale exact-base evidence.


## LIVE REFRESH — EPOCH-CONTINUOUS-0002 — 2026-09-07
CURRENT_MAIN: `c0ed340c8ee6ddfab011f861f5e6f49017ff5286` (merged independently audited #662 release archive/manifest/secret-safety).
LIVE_OVERRIDES_OLDER_SNAPSHOT: true.
BRANCH_PROTECTION: disabled; required-status enforcement off; release blocker.
DONE_DO_NOT_REPEAT: #641 durable batch cursor; #656 checker handoffs; #659 Windows autostart/settings; #651 durable model selection backend; #662 archive/manifest/secret-safety.
IN_PROGRESS: #654 Worker1 Pause/Resume/Cancel; #638 Worker1 recurrence; #674 Worker2 intelligence/model routing; #677 Worker3 exact Scenario-B composition; #663/#672 Worker4 Windows journey/shared UIA; #676 Worker5 install/update/rollback; #665 Worker5 governance evaluator.
BLOCKED: #654 current-main head `636b13523f604825f5c7b362bee812a5e114689c` has authoritative Core RED on confirmed user-Pause generation vs later _finish(PAUSED) rewrite; Worker1 owns repair and unchanged #669 replay. #672 shared packaged UIA is requalifying a read-only Observe/fresh semantic identity repair. Live governance is still administratively disabled.
NEXT_INTEGRATION_RULE: merge one unchanged exact-current production head only after all binding gates and independent same-head audit; QA_ONLY never merges; after merge reread main and invalidate stale evidence.
PACKAGE_FRONT: #676 exact head `0482f59ce1b15ddc25288bf5069c4972da37d00d` repairs installer directory/reparse control flow and is in fresh Core/M12; #665 Core green/M12 pending; #660 and SBOM/provenance lineages remain separate ownership/collision checks.
HUMAN_TESTED=false.
NVDA_VERIFIED=false.
PRODUCTION_RELEASE_READY=false.


EPOCH: EPOCH-CONTINUOUS-0001
STATUS: ACTIVE
STRATEGY: ONE_CONTINUOUS_FULL_PRODUCT
STARTING_MAIN: 7e8be7cb7ed3b62f55b1b9f5971bc45514427160
SOURCE_OF_TRUTH: live GitHub main/exact candidate heads/reviews/Actions > this file > Issue #553 ownership stream > Drive mirror > prompt snapshots

## Core strategy
Nika Core is one continuously developed Full Product. V0.1, V0.2 and later labels are acceptance checkpoints, not separate projects. Passing a checkpoint never stops development and never authorizes reimplementation of working components.

Primary optimization question:
**What most shortens the path to complete Full Nika while preserving the nearest acceptance checkpoint?**

Nearest checkpoint is V0.1. It remains binding so the project does not accumulate hundreds of unverified features.

## Mandatory cycle bootstrap
Every Worker, Work and Codex run must first read live main, AGENTS.md, MASTER_SPEC, ROADMAP, REUSE_CATALOG when present, ACCEPTANCE_GATES, PROJECT_STATUS, PARALLEL_EXECUTION_BOARD, this routing file, newest Issue #553 checkpoints, active owners/claims, open PRs/reviews, exact Actions and last exact green main.

Then classify each relevant component/previous assignment:
- DONE
- IN_PROGRESS
- STALE
- BLOCKED
- NEXT
- DO_NOT_REPEAT

One production writer per semantic slice. Reuse incumbent lineage. Shared contract edit requires an explicit compatibility decision. REUSE -> ADAPT -> CUSTOM(thin). GitHub live truth overrides stale routing.

## Current exact truth
At this routing publication base, live main is `7e8be7cb7ed3b62f55b1b9f5971bc45514427160`.
Core CI and M12 Pre-Human Release Gate are green on that exact main.
HUMAN_TESTED=false.
NVDA_VERIFIED=false.
PRODUCTION_RELEASE_READY=false.

Integrated/do-not-repeat foundation includes packaged three-agent execution, source setup, local/API ModelGateway, checker handoffs, durable provider/model selection backend, offline/reconnect and uncertain-effect safety, semantic browser stale-DOM/frame protection, durable batch cursor, Windows autostart backend + visible settings, canonical per-user data/recovery foundations.

## Nearest V0.1 remaining gates
1. Durable active Pause/Resume and restart/recovery truth. Incumbent #654 remains Worker1-owned; its newest repair addresses the independent successful-Pause return-boundary crash finding, but acceptance still requires fresh exact-head gates and non-self same-head audit.
2. Packaged startup recovery before shell exposure. Fresh current-main QA #652 proves scripts/nika_windows.py currently launches the shell without composing canonical RuntimeRecoveryService inventory/reconciliation. Repair must REUSE existing recovery authority and coordinate with current #663 launcher ownership or its immediate successor.
3. Recurrence/hibernate/missed-run lifecycle on current main.
4. One complete current-main browser Scenario B.
5. One complete current-main monitoring Scenario A.
6. User-visible packaged provider/model selector over the already-integrated canonical backend; #663 is the incumbent Windows lineage and prior-head evidence is stale after each repair.
7. Exact package/install/update/legacy-data/recovery/SBOM/notices/provenance qualification. #657 is superseded by current-main #660.
8. Binding repository governance; branch protection/required-status enforcement is currently disabled.
9. One exact Windows release candidate.
10. Human Windows/NVDA protocol after PRE_HUMAN_READY only.

Avoid cosmetic V0.1-only polish that final-product work will replace.

## Parallel Full Product rule
Safe independent Full Product work may proceed now only when:
- current ownership is disjoint;
- contracts are sufficiently stable;
- no current V0.1 gate is delayed;
- the work remains useful after the checkpoint;
- no second framework/authority is created.

After V0.1 passes, immediately continue the same codebase without waiting for Oleksii and without launching a separate V0.2 project.

Dependency-priority graph:
`runtime/durability -> AI/model layer -> browser/tools -> agent autonomy -> memory -> voice/perception -> resource governor -> background life -> learning/self-improvement integration -> phone/Telegram/multimodal surfaces -> full Windows/NVDA product journey -> final packaging/release`.

This graph guides priority, not artificial sequential phases; independent nodes may proceed in parallel.

## Worker 1 — Runtime + Audit
Nearest checkpoint: runtime durability, Pause/Resume, restart/recovery, recurrence/hibernate, cancellation and reconciliation.
Audit fail-safe: before new own source mutation, independently review the highest-value OTHER-lane exact-green production leaf waiting only same-head audit. Never self-audit Worker1-authored Stability source. Never merge.
Parallel/future: resource governor and background-life/runtime durability contracts when disjoint.

## Worker 2 — AI Layer
Nearest checkpoint: keep one canonical ModelGateway path; complete only missing model-choice/runtime/restart seams and narrow backend compatibility requested by Windows lane. Do not rebuild local/API ModelGateway or fork UI/settings.
Parallel/future: deterministic no-LLM, Foundry/Ollama/cloud parity, model identity/version/license/checksum/resource/cancellation contracts, experiment/benchmark hooks. No silent model download.

## Worker 3 — Browser + Tools
Nearest checkpoint: compose Scenario B on one current head, then Scenario A monitoring, reusing current semantic browser, ToolExecutor/effect authority, cursor/batch/readiness/report lineages.
Read-only JS/CDP/DOM diagnostics are allowed to understand unfamiliar sites but never become mutation authority or bypass approvals/security.
Parallel/future: controlled tool registry/MCP/capability-gap and agent-autonomy contracts. Never create a second browser/retry/cursor/monitor/effect system.

## Worker 4 — Windows Journey
Nearest checkpoint: visible provider/model selector over canonical backend, and canonical startup recovery inventory/reconciliation BEFORE shell exposure, plus truthful task/team lifecycle/recovery, clean package/install/restart, keyboard/UIA evidence and human protocol preparation. Current #663 owns the Windows launcher/UI slice; any startup-recovery repair on scripts/nika_windows.py must be coordinated into that lineage or an immediate successor.
Parallel/future: integrate memory/voice/multimodal/resource/background status into the Windows journey when those engines are owned elsewhere and contracts are stable. Do not create duplicate backend truth.

## Worker 5 — Coordinator + Integrator
Own guarded integration, package/release/governance, current routing/control and Drive mirror. Merge one production candidate at a time only after exact unchanged head, current base/overlap, required exact-head checks, required independent audit and no authoritative RED. Never self-audit or merge QA_ONLY.
After every merge or major Work/Codex finding, reread live state and reroute automatically.

## Work
Principal cross-system architect/auditor. Highest current use: independent audit of Worker1 Stability candidates and hard release/package/recovery cross-system defects not leased elsewhere. Major Work result triggers immediate routing refresh.

## Codex
Large coherent explicitly unleased implementation/reuse/dependency package. Before work, collision-check Worker1-5 and Work ownership. Persist major-phase checkpoints so another worker can resume after interruption.

## Governance
Release readiness is false while binding branch protection/required checks remain disabled or unproven. Do not weaken acceptance gates to obtain green.

## Human truth
Automated Windows/UIA/package proof may reach PRE_HUMAN_READY only.
HUMAN_TESTED=false until real human protocol.
NVDA_VERIFIED=false until real NVDA protocol.
