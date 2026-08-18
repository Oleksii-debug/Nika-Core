# M8 Controlled self-learning and experiment engine

Status: IMPLEMENTED candidate on `dev-b/m8-controlled-experiments`; no M8 product-weight credit until exact acceptance evidence is green and integrated.

## Reuse decision

- REUSE the already integrated Nika runtime/model/memory/resource contracts; M8 does not add a second orchestration or model gateway.
- REUSE Python deterministic arithmetic/statistics and typed dataclasses for baseline evaluation logic.
- REUSE the canonical SQLite persistence layer once an isolated storage-interface migration is accepted; this branch deliberately does not edit M1-M4 storage implementation.
- ADAPT DSPy later only for experiments that have an explicit dataset and metric. DSPy's official evaluation/optimizer model is compatible with Nika's metric-driven experiment boundary, but DSPy is not required for deterministic champion/challenger bookkeeping.
- CUSTOM (thin): immutable experiment identity, replay evidence, permission-fingerprint equality, promotion/guardrail policy, rollback evidence and repository port because these are Nika-specific product/safety semantics.

## Safety boundary

Experiment candidates are restricted to versioned prompt/strategy/config artifacts. There is no production-source mutation API in M8. Every challenger must carry the same permission fingerprint as the champion; a candidate that changes the permission boundary is rejected before the experiment can exist.

M8 does not authorize tool calls, external writes, financial actions, legal actions or destructive actions. Existing R4 execution-time approval remains authoritative.

## Deterministic evaluation

Each observation is bound to candidate + declared replay + declared metric. Duplicate observations for the same candidate/replay/metric fail closed. Completion requires minimum replay coverage for the primary metric and every guardrail for every candidate.

Promotion requires:
1. the challenger meets the configured minimum primary-metric improvement over the champion;
2. every guardrail stays within its maximum permitted regression;
3. replay coverage satisfies the policy.

If multiple challengers qualify, selection is deterministic by primary score then candidate ID. If none qualifies, the champion remains selected and the experiment completes without promotion.

Rollback is possible only after a recorded promotion and restores the recorded previous champion identity.

## Repository boundary and persistence blocker

`ExperimentRepository` is a stable port. `InMemoryExperimentRepository` is a deterministic development/test adapter only. Durable SQLite persistence is intentionally not faked in this branch because the canonical ordered migration registry belongs to the shared M1 storage surface. A separate isolated interface change is required for M8 schema v7 tables before durable persistence can be called IMPLEMENTED.

Until that storage-interface change is accepted, M8 is IMPLEMENTED for contracts/evaluation/promotion/rollback but PREPARED rather than complete for crash-safe persistence.

## Acceptance gate

Before M8 receives its 10% roadmap weight:

1. exact candidate passes dependency consistency, Ruff, compile and full pytest;
2. Ubuntu and Windows shared verification are green;
3. durable repository adapter uses accepted ordered schema migration and survives process recreation;
4. replay coverage, duplicate evidence, unknown candidate/replay/metric and permission-change tests pass;
5. promotion threshold and guardrail-denial tests pass;
6. rollback after process recreation passes against durable persistence;
7. no experiment path can silently widen permissions or rewrite production source;
8. exact SHA/CI evidence is recorded before integration.

`PACKAGED`, `HUMAN_TESTED` and `NVDA_VERIFIED` are separate later gates and are not claimed here.
