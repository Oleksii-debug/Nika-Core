# Product Factory worker path identity

Date: 2026-08-23  
Owner: MANUAL-DEV06  
Scope: Product Factory ↔ public `CodingWorkerPort` evidence adaptation.

## Compatibility decision

The Product Factory adapter does **not** add filesystem-case semantics to shared
`RepositorySnapshot`, `CodingJob`, `WorkspaceLease`, or DEV27 containment contracts.
DEV27 continues to own low-level workspace/process containment. The trusted Product
Factory host instead declares repository changed-file identity semantics through
`CodingWorkerDispatchContext.path_identity`.

This keeps framework-neutral Toolsmith contracts stable while removing the previous
implicit Windows-only assumption.

## Identity rules

1. Every worker changed path first reuses canonical Toolsmith
   `normalize_relative_path()`. Traversal, absolute paths, `.git` scope, drive/colon
   forms, and invalid repository-relative paths therefore remain fail-closed.
2. Separator aliases are the same identity on every supported repository semantic.
   `src/core/item.py` and `src\\core\\item.py` cannot be reported twice.
3. `CASE_SENSITIVE` permits distinct case variants such as `Foo.py` and `foo.py`.
4. `CASE_INSENSITIVE` treats those variants as one identity and rejects duplicates.
5. If a result contains case-variant paths and the trusted host did not declare a
   semantic, Product Factory blocks the evidence rather than guessing from the Nika
   coordinator OS. This matters for remote/container workers whose filesystem semantics
   can differ from the Windows-first coordinator host.

A result with no case-variant ambiguity remains backward-compatible when the declaration
is absent. This avoids forcing an unrelated shared-contract migration while still
failing closed at the exact point where case semantics affect authority.

## Preserved boundaries

- One `ComponentWorkRequest` still maps to one bounded `CodingJob`; a complete
  ProductProject is not converted into one worker job.
- Allowed-path checks, changed-file budget, exact base/result/diff evidence, independent
  review, cancellation reconciliation, typed failure classification, and stale-base
  rejection remain unchanged.
- Toolsmith capability-gap registration/resume remains component-scoped and exact.
- No new framework, persistence layer, sandbox, network permission, credential, or
  dependency is introduced.

## Acceptance evidence

The regression matrix requires:

- positive case-sensitive case variants;
- negative case-insensitive case variants;
- fail-closed undeclared case-variant semantics;
- negative separator alias even under case-sensitive semantics;
- existing worker lifecycle, cancellation, recovery, repair, evidence, and Toolsmith
  resume suites;
- exact-head Ubuntu and Windows Core CI plus complete M12 before integration credit.

Automated evidence does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.