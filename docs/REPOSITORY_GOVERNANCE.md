# Repository Governance

## Purpose

Nika Core release acceptance requires protected trusted-main provenance. The repository must not rely on an unreviewed direct push path or on branch-protection status names that can disappear, collide, or become successful merely because a conditional job was skipped.

This document defines the source-side prerequisite for safe branch-protection enforcement. It does not itself enable or modify GitHub repository settings.

## Stable Core required check

`.github/workflows/ci.yml` exposes one always-present job with the display name `Core required gate`.

The gate:

- depends only on the existing `verify` matrix;
- waits for both `ubuntu-latest` and `windows-latest` Core verification;
- runs with `if: ${{ always() }}` so a failed, cancelled, or skipped dependency cannot make the aggregate job disappear;
- exits successfully only when `needs.verify.result` is exactly `success`;
- does not depend on branch-specific Foundry, Ollama, M5, M9, M11, M12, media, or credential-store proof jobs;
- adds no checkout, credential, network, third-party action, permission, dependency, or secret surface.

The display name is intentionally unique across repository workflows because GitHub required-status-check selection is name based and ambiguous duplicate job names can block merging.

## Why optional proof jobs are not repository-wide required contexts

Nika Core has important conditional proof jobs that run only for specific branches or acceptance lanes. They remain mandatory when their acceptance gate applies, but they are not suitable as universal branch-protection status contexts because a context that is not produced consistently can deadlock unrelated pull requests.

The repository-wide minimum is therefore the stable `Core required gate`. M11, M12, PF3, DEV05, physical Windows, independent audit, and Product Journey evidence remain separate acceptance requirements according to the owning specification and lane. Passing the repository-wide Core gate never upgrades those requirements and never sets `HUMAN_TESTED` or `NVDA_VERIFIED`.

## Branch-protection activation boundary

Enabling or changing branch protection is a separate high-impact repository administration action. Do not perform it merely because this source change exists.

Before activation:

1. Merge the stable-gate source only after its exact candidate head passes current Core CI and the normal independent integration review.
2. Confirm `Core required gate` has completed successfully on the integrated repository within GitHub's required-check eligibility window.
3. Re-read live `main`, repository rulesets/branch protection, open integration work, and current check names immediately before the settings change.
4. Preview the exact proposed protection settings and obtain explicit project-owner approval for that settings mutation.

Initial safe enforcement target:

- branch: `main`;
- require pull requests before merging;
- require the unique `Core required gate` status check from GitHub Actions;
- disallow force pushes;
- disallow branch deletion;
- do not enable merge queue until `merge_group` triggering is deliberately added and qualified;
- do not treat repository protection as a substitute for M11/M12/PF/audit/human acceptance gates.

Whether administrators may bypass protection, how many approving reviews are required, and whether strict up-to-date status is enabled are policy decisions that must be previewed against the live parallel-development topology before activation. They are not silently chosen by this implementation lane.

## Rollback and incident handling

If branch protection blocks legitimate integration after activation, do not bypass it with a direct push or weaken product acceptance tests. Record the exact blocked status/context, identify whether the failure is a workflow-name/configuration defect or a real product failure, and repair the source/configuration through the same guarded path.

A temporary protection rollback, if ever required for repository recovery, is itself a high-impact administrative action and requires explicit owner approval plus an incident record. The preferred repair is to restore the stable required-check contract rather than normalize unprotected `main`.

## REUSE -> ADAPT -> CUSTOM (thin)

- **REUSE:** the existing Core CI Ubuntu+Windows `verify` matrix and the workflow supply-chain hardening integrated by PR #405.
- **ADAPT:** expose one stable aggregate status over that existing matrix.
- **CUSTOM (thin):** one fail-closed aggregate job, one deterministic regression contract, and this governance boundary document.

No second CI framework, policy engine, dependency, credential, or release authority is introduced.

## Truth flags

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`

`PRODUCTION_RELEASE_READY=false` until branch protection is actually enabled under explicit approval and all independent release gates are satisfied.
