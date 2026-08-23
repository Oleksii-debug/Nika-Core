# AI Trader DEV26 — held-out evidence integrity hardening

Lane: `MANUAL-DEV26`.
Current compatibility baseline during this cycle: `8e2e0eb3f0f65b75e1d23b0f36ab2bf09a8477ba`.

This dossier records two adversarial API-integrity families found before DEV26 acceptance.
Neither repair adds real-money authority, a dependency, or an execution route.

## Family 1 — unvalidated evidence construction

A caller could originally construct `SelectionDecision` directly and present chronology or
universe-cutoff state that had not passed `select_validation_candidate()`.

The repair makes selection and assessment evidence factory-created and adds repeated identity
validation:

- `SelectionDecision` is created through validated candidate selection;
- `HeldOutAssessment` is created through held-out binding;
- strategy and metric identities are canonical non-empty strings;
- dataset, universe, and protocol hashes are canonical lowercase SHA-256;
- metric evidence is a finite `Decimal` when present;
- metric direction must be an actual boolean;
- dirty data-quality evidence cannot be smuggled into selection state;
- fixed-universe membership must predate validation.

## Family 2 — post-bind chronology corruption

A later self-review found a second fail-closed gap. After a valid bind, low-level mutation of
`selected_at` or held-out result timestamps could survive `require_promotion_metric()` because the
assessment no longer retained the protocol windows needed to repeat chronology checks.

The repair binds a validated protocol snapshot into `HeldOutAssessment`. Every promotion-metric
read now revalidates:

- protocol structure and fingerprint;
- validation completion before selection;
- selection no later than held-out test start;
- fixed-universe cutoff strictly before validation;
- selected strategy, metric, dataset, universe, and quality identities;
- held-out fit cutoff no later than test start;
- held-out universe cutoff consistency;
- held-out metric finalization no earlier than test completion.

A dedicated adversarial test mutates `selected_at` and `evaluated_at` after a successful bind and
requires fail-closed rejection at promotion-metric access.

## Preserved protocol semantics

The protocol keeps half-open boundary compatibility: selection at the exact shared
`validation.end_at == test.start_at` instant is allowed, while any later selection is rejected.
No test score participates in candidate selection. Missing held-out metric evidence cannot become
promotion evidence.

## Ruff repair lineage

Earlier exact Ubuntu/Windows Core verification exposed four DEV26-owned findings: `FLY002`,
`I001`, `RUF007`, and `PLR1730`. They were repaired without ignores or gate weakening using an
equivalent f-string protocol payload, canonical imports, `itertools.pairwise()`, and `max()`.
Protocol fingerprint compatibility was checked across that repair.

## Local evidence before final exact-head CI

- prior complete focused DEV26 family: 21 tests passed;
- deterministic metric property matrix: 500 equity paths passed;
- candidate tie-order permutations: 6 of 6 passed;
- new chronology-integrity harness: 7 tests passed;
- changed source and integrity test compile successfully;
- AST unused-import check is clean;
- maximum changed line length is 100 characters;
- local Git blob hashes matched the source/test blobs committed to GitHub.

Exact-head Core CI and complete M12 remain authoritative for repository Ruff, compile, import,
full-suite, Ubuntu, and Windows acceptance. Independent AUD03 replay is still required before
integration. Automated evidence does not set human accessibility truth.

`REAL_MONEY_AUTHORITY=false`.
`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
`PRODUCTION_RELEASE_READY=false`.
