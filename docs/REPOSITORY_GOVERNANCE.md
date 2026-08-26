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

## Trust boundary of a status-check gate

The stable gate fixes required-check availability and fail-closed aggregation. It is **not** an immutable security authority by itself.

For a normal GitHub Actions `pull_request` run, the workflow definition is taken from the pull request merge commit. A pull request that changes `.github/workflows/ci.yml` can therefore change the workflow that produces the `Core required gate` check. Read-only workflow permissions and `persist-credentials: false` reduce credential exposure, but they do not make candidate-supplied workflow logic trusted.

Consequences:

- branch protection using `Core required gate` is a strong minimum against accidental direct-main updates and against skipped/conditional-check deadlocks;
- it must not be described as proof that a malicious or incorrectly authorized workflow-changing pull request cannot manufacture its own CI behavior;
- ordinary product lanes must not modify `.github/workflows/**`;
- workflow changes require an explicit governance/security lane, current-main reread, exact diff review, and independent integration decision;
- if the live GitHub plan/account topology supports an independently enforced required-workflow or file-path protection mechanism, it should be evaluated as a separate governance hardening step rather than silently assumed here.

Nika Core currently uses one GitHub user repository identity for repository administration, so GitHub account-level review metadata cannot be treated as a substitute for the project's independent TECH02/AUD review process.

## Branch-protection activation boundary

Enabling or changing branch protection is a separate high-impact repository administration action. Do not perform it merely because this source change exists.

Before activation:

1. Merge the stable-gate source only after its exact candidate head passes current Core CI and the normal independent integration review.
2. Confirm `Core required gate` has completed successfully on the integrated repository within GitHub's required-check eligibility window.
3. Re-read live `main`, repository rulesets/branch protection, open integration work, current check names, and any open workflow-changing pull requests immediately before the settings change.
4. Preview the exact proposed protection settings and obtain explicit project-owner approval for that settings mutation.

Initial bounded enforcement target:

- branch: `main`;
- require pull requests before merging;
- require the unique `Core required gate` status check from GitHub Actions;
- disallow force pushes;
- disallow branch deletion;
- do not enable merge queue until `merge_group` triggering is deliberately added and qualified;
- do not treat repository protection as a substitute for M11/M12/PF/audit/human acceptance gates;
- do not claim workflow-file immutability unless a separate independently enforced mechanism actually provides it.

Whether administrators may bypass protection, how many approving reviews are required, whether strict up-to-date status is enabled, and whether an additional workflow-file protection mechanism is available are policy decisions that must be previewed against the live parallel-development topology before activation. They are not silently chosen by this implementation lane.

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
