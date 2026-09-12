# Global Coordinator Bridge

Nika Core participates in the owner's cross-project autonomous development control plane.

## Purpose

Workers, Codex Cloud and Work must not rely on stale prompt text as the current project plan. Before substantive work, reconstruct the newest Nika state and read current coordination surfaces. The coordinator changes the shared plan; Oleksii should not have to manually rewrite worker prompts after every major development run.

## Coordination hierarchy

1. Global coordinator master: cross-project priorities for Nika Core, 12-6 AI, Accessible Chess and future projects.
2. Project-local orchestration plan: current Nika priorities, completed work, do-not-repeat items, active ownership and next unowned packages.
3. Worker execution: each run performs only current useful work and leaves durable evidence.

## Audit cadence

Do not depend on exact hours such as 06:00/12:00/18:00. If the configured audit interval has expired, the first available coordinator-capable run performs the audit and refreshes the plan. A missed run because of usage/credits is recovered by the next successful run.

Suggested active-project audit interval: 4–6 hours, plus refresh after a major integration or release-gate change.

## Codex Cloud

Codex reads the current Nika project plan before choosing work. It must avoid packages already assigned to scheduled workers, take a large unowned package, persist progress after coherent milestones and update project direction when its work materially changes the next critical path.

## Work

Work is the periodic high-capability architect/auditor. It can re-evaluate the whole product, find stale directions, and update the shared plan. It should not require Oleksii to copy new prompts into every scheduled worker.

## Nika current product priority

Finish the first genuinely usable Windows/NVDA Nika Core release before allowing long-term Living Agent work to destabilize V0.1. Living-agent voice, embodiment, 12-6 integration and autonomous-learning capabilities are added in staged increments around a stable usable core.

## Owner-facing reporting

Oleksii is not expected to manage GitHub mechanics. Reports should say in plain Ukrainian: what is ready, what works, what does not work yet, what changed, current readiness, next user-visible milestone, and whether owner approval/budget is required.
