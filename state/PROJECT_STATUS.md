# PROJECT STATUS — Nika Core

Reconciled: 2026-08-26.
Purpose: durable product/acceptance truth and the protocol for resolving live state.
This file is **not** a live GitHub cache.

`LIVE_GITHUB_PRECEDENCE=true`
`NON_AUTHORITATIVE_SNAPSHOT=true`

## Authority and freshness

Every worker must resolve volatile state immediately before acting. Use this order:

1. live GitHub `main` and the exact current commit SHA;
2. `AGENTS.md`, `docs/MASTER_SPEC.md`, `docs/ROADMAP.md`,
   `docs/REUSE_CATALOG_2026-08-18.md` when present, and
   `docs/ACCEPTANCE_GATES.md` from that exact `main`;
3. latest Issue #1 coordination markers, open PR heads/files, current branches and current Actions;
4. `state/PARALLEL_EXECUTION_BOARD.md` for the durable collision protocol;
5. Drive/manual handoffs for context only after checking them against live GitHub.

If any static document, Drive snapshot, PR body, old comment, artifact, or historical SHA
conflicts with current GitHub state, current GitHub wins.

A SHA, PR number, owner list, CI run, mergeability result, branch reservation, or percentage
written in a Markdown file is historical evidence unless it was re-read live in the current cycle.

## Durable product truth

Nika Core is an active Windows/NVDA-first agent platform and Autonomous Product Factory project.
The expanded Full Product Vision and PF0-PF12 acceptance model are binding; historical milestone
percentages and old packaged ZIPs do not prove the expanded product.

Current release/human truth remains fail-closed until fresh evidence proves otherwise:

- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- no automated test may set `HUMAN_TESTED` or `NVDA_VERIFIED`;
- no stale package or branch-head result may be promoted as current release evidence.

Product Factory completion requires the representative end-to-end journey defined by the
acceptance documents, not merely backend contracts or isolated subsystem tests. Required
human-only evidence remains human-only.

## Reconciliation snapshot — non-authoritative after any live change

This snapshot records the basis used for the 2026-08-26 anti-staleness repair. It is not a
replacement for the live reads above.

- reconciliation base `main`: `109829579ab4693e038e218769c23c2547defd64`;
- that base is merge PR #405, shared workflow supply-chain security convergence;
- exact-base hosted evidence observed during reconciliation:
  - Core CI `32980430285`: SUCCESS;
  - M11 `32980430222`: SUCCESS;
  - M12 Pre-Human Release Gate `32980430229`: SUCCESS;
- branch protection on `main` was observed as disabled and repository rulesets as empty;
- live development had advanced far beyond the old #90-#98 status snapshot, with current
  production, QA, audit and integration lanes coordinated through GitHub.

Do not use this snapshot to decide current ownership, mergeability, current CI state, or current
`main`. It expires for those purposes as soon as any relevant GitHub state changes.

## Evidence states

Use the repository-wide evidence vocabulary without promotion by implication:

- PREPARED — scope/contracts/reuse decision are ready.
- IMPLEMENTED — production-intended source/tests exist on a branch.
- GREEN — the exact candidate head passed all required automated gates.
- INTEGRATED — that exact accepted candidate was merged into `main`.
- PACKAGED — an installable artifact was built and verified against its exact source identity.
- HUMAN_TESTED — a person completed the required manual protocol.
- NVDA_VERIFIED — a person completed the required NVDA protocol.

A later commit invalidates exact-head GREEN credit until the required gates rerun on the new head.
A main move can invalidate compatibility/integration readiness even when the candidate itself did
not change.

## Product Factory and integration truth

PF0-PF12 status is determined from current integrated source, exact open-PR heads, current
acceptance evidence and current dependency relationships. Do not maintain a static owner/PR roster
in this file: it becomes unsafe under parallel development.

Before consuming another lane:

- require its public contract to be integrated or an explicit dependency/base relationship;
- do not import an unrelated unmerged sibling branch as canonical truth;
- preserve one canonical authority for each durable/security/recovery domain;
- re-check current-main ancestry and exact-head gates immediately before integration.

Blocked work in one lane does not block independent non-conflicting lanes.

## Pre-human change policy

The active pre-human policy remains conservative:

- do not add unrelated feature scope merely because a branch is available;
- concrete defects, acceptance/evidence repairs, compatibility convergence, security/reliability
  repairs and coordination maintenance may proceed under their normal owner/gate rules;
- do not weaken an acceptance gate to obtain green;
- use `REUSE -> ADAPT -> CUSTOM (thin)` and supported upstream components before custom framework
  work.

If a newer binding specification changes this policy, the newer live specification wins and this
file must be reconciled rather than treated as higher authority.

## Worker cycle protocol

Before each substantive cycle:

1. re-read live `main` and all mandatory documents from that SHA;
2. inspect Issue #1 latest markers, open PRs, active branches and exact Actions;
3. detect both path collisions and semantic/shared-authority collisions;
4. select an unowned coherent lane;
5. branch from the exact compatible base;
6. post an ownership marker before production/shared-contract edits;
7. after changes, run applicable dependency/lint/compile/tests and exact hosted gates;
8. report the exact head, what is and is not proven, and never self-award human/NVDA evidence.

## Maintenance rule for this file

Update this file for durable product/acceptance truth or a deliberate reconciliation record.
Do **not** turn it back into a manually maintained live owner board. Volatile ownership belongs in
live Issue #1 markers, branches/PRs and Actions; volatile technical truth belongs in live GitHub.
