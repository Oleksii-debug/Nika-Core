## 2026-10-11 Owner work decomposition (no renumbering)
Each of the seven numbered Drive plans now contains A/B/C micro-closure checkpoints inside its first Section. A = reuse audited existing implementation + exact SHA/scoped tests; B = first provable failed CI job repaired with negative/recovery tests; C = full inherited acceptance, ordered integration and postmerge main SHA readback. Independently record A/Б/В status, but original Section DONE only when all required evidence exists. Do not create a separate new PR or reimplement old code to satisfy the checkpoints; they are acceptance bookkeeping, not expanded feature scope. No prompt/session changes. Terminal total section counts remain unchanged.

# Nika Core — Canonical Multi-Plan Index

## Authority
The former monolithic 50-Section plan (0–49) is SUPERSEDED FOR WORK SELECTION and remains audit/history only.

Canonical Drive folder:
https://drive.google.com/drive/folders/1ncMMl9ce2Wq6vnsMI9PxJS7-s4yq4Iam

Plan files use the universal naming convention:
1. Перший план
2. Другий план
3. Третій план
4. Четвертий план
5. П’ятий план
6. Шостий план
7. Сьомий план

Plans 1–6 are independent engineering plans with no global priority order.
Plan 7 is final whole-product convergence / physical acceptance / documentation / go-live and MUST NOT be started as a normal worker lane before required terminal outputs from Plans 1–6 exist.

## Plan themes
1. Core architecture, contracts, persistence, runtime, security, capability/model authorities.
2. Agent platform and Autonomous Product Factory.
3. Deterministic/model intelligence, memory, research and self-learning.
4. Windows product journey, browser/UIA, accessibility, media and reports.
5. Domain workspaces: Trader, Personal Nika, Business Factory.
6. Web/Cloud/Node, packaging, provenance, diagnostics and performance.
7. Whole-product convergence, physical Windows/NVDA/Web/Cloud acceptance, documentation and go-live.

## Worker selection
For Plans 1–6:
- read this file, MULTI_PLAN_PARALLELISM_CONTRACT.md, MULTI_PLAN_CLOSURE_STATE.md, AGENTS.md, the assigned Drive plan and live GitHub;
- skip terminal DONE;
- take the first unfinished ACTIONABLE Section inside the assigned plan;
- audit/reuse existing implementation/PR/evidence first;
- implement only missing acceptance-critical gaps;
- use stable contracts/fixtures for unfinished peer plans;
- do not claim whole-product evidence from mocks/fixtures;
- do not silently mutate another plan.

Plan 7 is dependency-bound and receives no ordinary parallel worker prompt until Plans 1–6 are sufficiently terminal for convergence.

Detailed mapping: LEGACY_50_TO_MULTIPLAN_COVERAGE.md.
