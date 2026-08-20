# PF8 / PF9 reuse qualification — 2026-08-20

This document is part of the same AUTO-PF4 deep Product Factory acceptance batch as
`PRODUCT_FACTORY_PF4_ACCEPTANCE_MATRIX_2026-08-20.md`. It does not create a second Operations
runtime or Business Factory. It identifies what Nika already has, proves reusable invariants, and
keeps absent Product Factory lifecycle requirements visibly unqualified.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PF11=false`.

## PF8 — Operations and maintenance

### Binding acceptance target

PF8 is not "there is a rollback method somewhere". A qualifying Product Factory operations cycle
must durably connect at least:

1. a concrete ProductProject/release identity;
2. an incident or maintenance trigger;
3. evidence of impact/current state;
4. a bounded repair candidate;
5. independent acceptance/review evidence;
6. a new exact release identity;
7. staged deployment/health evidence;
8. monitor/reconcile evidence;
9. rollback to the exact prior release when health fails;
10. durable incident/repair/release history across restart.

### Current reusable Nika surfaces

Nika already has useful pieces and PF8 should reuse them instead of creating parallel mechanisms:

- PF1 durable ProductProject references include release/deployment/incident reference fields;
- PF2/PF4 coordinator and coding-worker contracts represent bounded repair work and independent
  review;
- PF3 DeploymentFabric represents deploy, health, uncertain reconciliation and rollback evidence;
- M8 Experiment Engine can evaluate a repair strategy with replay evidence while preserving a fixed
  permission fingerprint;
- canonical SQLite/audit infrastructure can provide durable state once the PF8 lifecycle contract is
  owned and integrated.

### What is not currently integrated

At the inspected Product Factory main there is no dedicated durable incident/maintenance aggregate,
no public ProductProject incident-write lifecycle, and no composition boundary that turns a detected
incident into `incident -> bounded repair -> review -> release -> staged deploy -> monitor -> rollback`
with one restart-safe identity chain.

Therefore PF8 is **NOT PROVEN**, even though individual PF2/PF3/M8 primitives exist.

### Executable reuse proof added by PF4

`tests/test_product_factory_acceptance_pf8_pf9.py` proves that the existing M8 engine rejects a repair
challenger that changes the maintenance permission fingerprint. This is deliberately a narrow reuse
proof: it demonstrates that repair experimentation can remain sandboxed. It does not claim incident
management or deployment completion.

### External reliability baseline

Primary source: Google SRE Workbook, Incident Response:
https://sre.google/workbook/incident-response/

The current useful invariants are structured response, explicit roles/coordination, a working record
of debugging/mitigation, and restoring service/mitigating impact rather than treating an incident as
an ordinary background task.

Primary source: Google SRE Workbook, Canarying Releases:
https://sre.google/workbook/canarying-releases/

The useful Product Factory implications are partial/time-limited exposure, evaluation before broader
promotion, monitoring of rollout state, and cheap rollback of small self-contained releases when
health signals regress.

Primary source: Google SRE Workbook, Configuration Design and Best Practices:
https://sre.google/workbook/configuration-design/

A particularly relevant invariant is that rollback is often safer/faster than inventing a live patch
under outage pressure, and rollback requires a sufficiently hermetic exact prior configuration/release.

PF8 acceptance should encode those invariants without pretending that Google-specific operational
process is itself a Nika implementation dependency.

## PF9 — Business Factory sandbox

### Binding acceptance target

PF9 must not mean "the model can write marketing text". A qualifying sandboxed business-factory
vertical needs a typed ProductProject/business experiment with at least:

- hypothesis;
- champion/control and challenger identity;
- immutable source/artifact reference;
- fixed permission boundary;
- replay or explicitly scoped audience/segment identity;
- primary success metric;
- guardrail metrics;
- complete evidence coverage;
- deterministic promotion decision;
- durable restart;
- rollback;
- explicit approval before any external purchase, payment, domain, DNS, hosting or account mutation.

### Existing reusable M8 Experiment Engine

The integrated M8 engine is a strong foundation rather than something PF9 should rewrite:

- `ArtifactKind` supports `CONFIG`, `STRATEGY`, and `PROMPT` candidates;
- champion/challenger candidates have immutable IDs, versions and artifact references;
- all candidates must preserve one `permission_fingerprint`;
- replay cases have stable dataset reference/version identity;
- observations are finite, typed by candidate/replay/metric and duplicate-protected;
- completion requires complete replay coverage;
- a primary metric controls improvement;
- guardrails can veto a challenger despite primary-metric gain;
- deterministic promotion records prior champion;
- rollback restores the recorded prior champion;
- `SQLiteExperimentRepository` persists definition, append-only observations and state/event history;
- existing repository tests already prove restart, atomic transition/event rollback, immutable evidence,
  stale-writer protection and illegal-transition rejection.

### New PF4 cross-domain qualification

PF4 adds a business-shaped CONFIG experiment rather than inventing a new engine:

- champion: `business://pricing/v1`;
- challenger: `business://pricing/v2`;
- three stable segment replays;
- primary metric: conversion;
- guardrail: compliance;
- SQLite restart occurs mid-experiment;
- a compliant conversion improvement promotes;
- a later process can roll back to the recorded champion;
- a challenger that asks for a wider `permission_fingerprint` is rejected;
- a challenger with much higher conversion but worse compliance does not promote.

This proves **PF9 reusable sandbox foundation**, not PF9 completion.

### External experimentation baseline

Microsoft research on controlled rollouts emphasizes guardrail metrics as constraints that should not
regress even when local/success metrics improve:
https://www.microsoft.com/en-us/research/uploads/prod/2020/06/Safe-Velocity-ICSE-SEI.pdf

Microsoft PlayFab experiment terminology similarly distinguishes hypothesis, segment, control and
conversion concepts:
https://learn.microsoft.com/en-us/xbox/playfab/live-service-management/game-configuration/experiments/experimentation-key-terms

The Nika M8 engine already captures the most important generic safety shape: control/challenger,
fixed permissions, replay identity, primary metric, guardrails, promotion and rollback. PF9 should
adapt this surface instead of creating a second experiment database.

### What remains absent for PF9 completion

The current Product Factory does not yet prove:

- a durable ProductProject -> business experiment link;
- typed business artifacts such as landing page/funnel/pricing package with provenance;
- budget ceilings integrated with the experiment;
- privacy/compliance classification for real audience data;
- external action approval for domain/DNS/hosting/payment/account creation;
- simulated checkout/payment boundary;
- staging/preview publication identity;
- packaged Command Center presentation and user approval path;
- a full PF9 -> PF10 -> PF11 handoff into licensed/reviewed release evidence.

Until those exist and are independently tested, the correct classification is
**FOUNDATION REUSED / PRODUCT-INTEGRATION NOT PROVEN**, not `PF9=true`.

## No-duplication decision

PF8 and PF9 should share, not duplicate:

- ProductProject identity and durable audit;
- PF2 bounded work/review;
- PF3 execution/deployment evidence;
- PF7 scoped credential leases;
- M8 Experiment Engine for replay/promotion/rollback;
- common approval/permission infrastructure for external side effects.

This keeps Nika's architecture aligned with the Product Factory rule: build the factory, not a new
one-off runtime for every product or business experiment.
