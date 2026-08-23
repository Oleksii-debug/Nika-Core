# AI Trader DEV26 — held-out evidence integrity hardening

Exact parent candidate: `3272b609e6b92de67a70f27f8c9c11badf8035eb`.
Lane: `MANUAL-DEV26`.

This follow-up closes an adversarial API-integrity gap found before accepting CI evidence.
A caller could previously construct `SelectionDecision` directly and then present chronology or
universe-cutoff state that had not passed `select_validation_candidate()`.

The repair keeps the original no-real-money scope and adds no dependency:

- `SelectionDecision` is factory-only through validated selection;
- `HeldOutAssessment` is factory-only through held-out binding;
- held-out binding repeats validation-completion and fixed-universe chronology checks;
- promotion-metric access revalidates selection/result identity after construction;
- strategy/metric identities must be canonical non-empty strings;
- dataset, universe and protocol hashes must be canonical lowercase SHA-256;
- metric evidence is a finite `Decimal` when present;
- metric direction must be an actual boolean;
- dirty data-quality evidence cannot be smuggled into a selection object;
- a universe cutoff at validation start is rejected: membership must be fixed beforehand.

The protocol retains half-open boundary compatibility: selection at the exact shared
`validation.end_at == test.start_at` instant is allowed, while any later selection is rejected.

Local focused evidence for the complete DEV26 batch after this hardening:
21 adversarial tests pass, Python compilation passes, and all changed lines are at most 100
characters. Ruff is not installed in the authoring runtime; exact-head Core CI remains authoritative.

`REAL_MONEY_AUTHORITY=false`.
`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
