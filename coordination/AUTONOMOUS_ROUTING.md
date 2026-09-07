# Nika Core — current autonomous routing

EPOCH: EPOCH-0005
STATUS: ACTIVE
AUDIT_TIME: 2026-09-07T10:50:00+02:00
AUDIT_BASE_MAIN: 536d50b1d567fdeae17fc787ac461921f7a1ee04
LAST_GLOBAL_AUDIT: 2026-09-07T10:50:00+02:00
NEXT_AUDIT_RULE: first eligible coordinator-capable worker after >=6h, or immediately after a major integration/blocker/Windows-NVDA/package-readiness change
FAILOVER_AUDIT_RULE: another capable worker may refresh after approximately 8h without a valid audit
CURRENT_VERSION: V0.1_ONLY_UNTIL_RELEASE

## Source-of-truth hierarchy
1. Live GitHub main, exact candidate heads, reviews and Actions.
2. This routing file; while this PR is pending, a newer Issue #553 checkpoint wins.
3. Issue #553 ownership/event stream.
4. Drive owner-readable mirror when available.
5. Stable worker prompts define home lanes only, never exact PR/SHA priority.

## Customer-visible V0.1 truth
Integrated production lineage now includes the durable Scenario-B cursor (#641), ModelGateway checker handoffs (#656), and packaged Windows autostart/settings UX (#659). The product is not release-ready: packaged durable model selection, full Scenario-B composition, monitoring/recurrence, durable active Pause/Resume, final package/install/update/recovery/SBOM/notices/provenance, repository governance, and human Windows/NVDA acceptance remain open.

HUMAN_TESTED=false
NVDA_VERIFIED=false
PRODUCTION_RELEASE_READY=false

## Integrated / DO NOT REIMPLEMENT
- #641 durable batch cursor/current-input/crash-order wake lineage.
- #656 existing ModelGateway three-agent checker-result handoff composition.
- #659 Windows autostart/settings UI and packaged automated UIA/keyboard/restart evidence.
- Existing local Ollama + configured API ModelGateway paths and credential-reference boundary.
- Existing startup/recovery, offline/reconnect, terminal scheduler authority, semantic browser stale-DOM/frame/document authority, canonical per-user DB and legacy-adoption foundations.

## Top blockers / current release frontier
1. #651 packaged durable provider/model selection must converge from stale base 3f8b9dc6 onto exact current main 536d50b1. Its delta is still exactly three additive files and has zero path overlap with the #659 main movement; old Core/M12 green is lineage-only. Fresh exact-head Core+M12 and Worker1 independent same-head audit are required.
2. #654 durable active Pause/Resume is Worker1-owned Stability source on an older base. It requires current-main convergence, fresh gates, then Work/manual/other valid independent audit; Worker1 cannot self-audit it.
3. Browser Scenario B still lacks one exact-current integrated composition. Existing #647/#643/#644/#645/#646 leaves are stale-base/open lineages; do not accumulate old greens as acceptance.
4. Monitoring Scenario A remains dependency-gated by accepted recurrence/durable observation composition; do not create a second scheduler/monitor engine.
5. #657 packaged legacy-data proof is stale-base and not merge-ready; historical M11/M12 package failure is not release credit. Repair/converge only in the incumbent package lane.
6. Repository governance is a binding release blocker: main branch reports protected=false, protection disabled, required status-check enforcement off.
7. Human Windows keyboard/NVDA test remains final authority after PRE_HUMAN_READY only.

## Worker 1 — Stability / independent-audit fail-safe
HOME: long-task continuity, Pause/Resume, restart, recurrence/hibernate, no duplicate effects.
CURRENT: converge #654 onto exact current main without rebuilding scheduler/recovery/effect authority. Before new Stability mutation, audit any exact-current external lane leaf that is terminal-green and waiting only independent review. #651 is the next expected external audit once reconverged and green. Never self-audit #654 or other Worker1-authored Stability source.

## Worker 2 — AI / ModelGateway composition
HOME: local/API AI, provider/model config, ModelGateway product path.
CURRENT: highest priority is exact-current convergence of #651 preserving the unchanged three-file semantic delta and integrated #656 runtime. No ModelGateway/provider rewrite, no Worker4 shared UI mutation, no silent provider fallback, no secret-bearing durable state. After convergence: fresh Core+M12, publish exact SHA/base, route Worker1 independent audit.

## Worker 3 — Browser / batch / monitoring
HOME: browser, bounded batch, tabs, readiness, cursor, per-target report, monitoring.
CURRENT: do not fork stale #647 or build a second cursor. Reconstruct the current-main Scenario-B composition using integrated #641 plus accepted/reconverged tabs/max5/readiness/report leaves; repair only the earliest demonstrated composition defect. Monitoring remains after recurrence/Scenario-B authority is clear.

## Worker 4 — Windows / accessibility / package readiness
HOME: Windows 11, keyboard/UIA, WebView2, packaged settings, install/restart proof.
CURRENT: #659 is integrated; STOP repeating autostart lineage. Next product seam is accessible provider/model selection UI only after #651 backend acceptance, consuming the existing DesktopBackend/UIActionBridge/Action Registry shell. In parallel, own/fix package build/recovery evidence such as #657 only within the incumbent package lane. Automated UIA never sets NVDA_VERIFIED.

## Worker 5 — Integrator / release coordinator
HOME: guarded merge, dependency conflict resolution, exact Windows RC, package/recovery/provenance/governance, periodic audit.
CURRENT: no merge-ready leaf at audit time. Integration rehearsal for #651 proved DIVERGED current-main relation but zero path overlap with #659 movement, so owner convergence is mechanically low-risk. Merge one production candidate at a time only after exact unchanged head/current base/required same-head terminal gates/independent audit/mergeability are re-read immediately before merge. Never merge QA_ONLY.

## Codex Cloud
No confirmed active package in the newest live checkpoint. Next useful disjoint package: final package/recovery/governance composition or stale-lineage retirement/dependency map, but do not duplicate Worker2 #651, Worker1 #654, or Worker3 Scenario-B ownership. Checkpoint every coherent phase.

## Work / principal architect-auditor
Use Work for a deep cross-system release audit or independent Stability audit when #654 becomes exact-current/green. A major Work finding is an immediate routing-refresh trigger.

## Collisions / stale assignments
- #651 belongs to Worker2; Worker5 must not mutate its source.
- #654 belongs to Worker1 Stability; Worker1 cannot provide its independent release clearance.
- #659 autostart/settings is integrated; any assignment repeating #639/#659 is STOP_STALE.
- #647 and sibling Scenario-B leaves are stale-base evidence, not current acceptance.
- QA_ONLY branches are NEVER_MERGE.

## Integration order
1. #651 exact-current convergence -> fresh Core/M12 -> independent Worker1 PASS -> guarded Worker5 merge.
2. Re-read main; invalidate stale downstream evidence.
3. #654 exact-current convergence -> external independent audit -> guarded merge when qualified.
4. Current-main Scenario-B composition and monitoring/recurrence convergence.
5. Exact package/install/update/recovery/SBOM/notices/provenance + governance proof.
6. PRE_HUMAN_READY only after all automated binding gates.
7. Oleksii human Windows keyboard/NVDA protocol; repair any human-found defects; only then HUMAN_TESTED/NVDA_VERIFIED may change.

## Governance
Release readiness cannot be true while branch protection/required checks are unproven or disabled. Current live main reports protected=false, protection.enabled=false, required_status_checks.enforcement_level=off.

## Audit cadence
LAST_GLOBAL_AUDIT: 2026-09-07T10:50:00+02:00
NEXT_FULL_AUDIT_NOT_BEFORE: 2026-09-07T16:50:00+02:00 unless a major trigger occurs
FAILOVER_AUDIT_AFTER: approximately 2026-09-07T18:50:00+02:00 if no valid refresh exists

## Коротко для Олексія
У main уже є durable Scenario-B cursor, реальний ModelGateway checker handoff і Windows autostart/settings UX. Найближчий корисний крок — не новий фреймворк, а чиста current-main конвергенція #651 з новими Core/M12 та незалежним аудитом, після чого Worker5 може зробити один guarded merge. Далі — durable active Pause/Resume, повна browser/monitoring композиція, package/recovery/governance і лише потім реальний human Windows/NVDA тест.
