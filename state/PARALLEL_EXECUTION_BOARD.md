# PARALLEL EXECUTION BOARD — Nika Core

Reconciled: 2026-08-26.
Mode: **ACTIVE PARALLEL DEVELOPMENT WITH LIVE COLLISION CONTROL**.
Purpose: durable coordination protocol, not a static roster of current workers.

`LIVE_GITHUB_PRECEDENCE=true`
`NON_AUTHORITATIVE_SNAPSHOT=true`

## Live board resolution order

Resolve the board anew immediately before every write, review handoff, rebase/sync, or integration
decision:

1. live `main` SHA;
2. latest Issue #1 ownership/handoff markers;
3. open PR heads, bases and changed files;
4. live branches, including recent branches that do not yet have a PR;
5. current GitHub Actions/checks for the exact candidate SHA;
6. mandatory repository specifications and acceptance gates at the exact live `main`;
7. Drive/manual snapshots only as historical/contextual input.

A static line in this file never reserves a production slice.

Treat a branch-without-PR as a possible active reservation when its name/scope and recent commit
activity overlap the proposed lane. Treat an old marker as historical until its latest status,
branch and activity are checked. If ownership is ambiguous, yield from the overlapping slice or
choose an independent lane rather than creating a second writer.

## Ownership marker contract

A worker taking a coherent lane should publish a live marker before shared/production edits. The
marker should identify, as applicable:

- `STATUS`;
- exact `START_MAIN` or exact dependency parent;
- branch;
- role/claim;
- `OWNERSHIP_PATHS`;
- semantic/shared authorities that must not be duplicated;
- compatibility decision for shared-contract edits;
- `REUSE_ADAPT_CUSTOM`;
- safety/release limits;
- `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` unless real human evidence exists.

Issue #1 is the default coordination thread for these markers. A PR is the canonical review vehicle
once code/evidence exists. Branch names alone are not sufficient when a marker can be posted.

## One-writer collision rules

1. One production writer per owned slice.
2. Separate branch per independent coherent lane.
3. Check both exact paths and semantic authority before editing.
4. Do not stack unrelated branches.
5. Do not edit another lane merely because its branch is stale; first establish an explicit
   ownership transfer/supersession decision.
6. Shared-contract edits require an explicit compatibility decision and focused regression proof.
7. A blocked lane does not idle independent lanes.
8. No direct worker write to `main`.
9. No self-merge merely because owner CI is green.
10. Re-read live ownership immediately before the first write and immediately before integration.

If two lanes collide after both started, the later/less-authoritative lane yields unless the owners
publish a compatibility decision that makes the split explicit.

## Dynamic/stale marker handling

`STATUS=IN_PROGRESS` is not an eternal lock. Determine whether it is still active from the newest
Issue #1/PR comments, branch head activity, replacement/supersession markers and dependency state.

Conversely, the absence of an open PR does not mean a slice is free. Recent reservation branches
and fresh Issue #1 markers must be checked before work starts.

Never infer ownership from an old Drive lane table when live GitHub shows a newer branch, PR or
marker.

## Evidence states

- PREPARED — scope/contracts/reuse decision ready.
- IMPLEMENTED — production-intended source/tests exist on a branch.
- GREEN — exact candidate head passed required automated checks.
- INTEGRATED — exact accepted candidate merged into `main`.
- PACKAGED — exact installable artifact built and verified.
- HUMAN_TESTED — a person completed the required manual protocol.
- NVDA_VERIFIED — a person completed the required NVDA protocol.

Owner tests do not automatically satisfy an independent audit requirement. QA-only branches do not
become production code. A head move invalidates exact-head evidence until gates are rerun.

## Shared authority and compatibility

`REUSE -> ADAPT -> CUSTOM (thin)` is mandatory for generic capability work.

Do not create a second authority for an existing responsibility such as durable project state,
approval, credential storage, recovery, task lifecycle, model routing, resource accounting,
release provenance, Product Factory checkpoint authority, or accessibility action semantics.

When a proposed change touches a shared contract:

1. identify current consumers and active owners;
2. state the compatibility decision in the live marker/PR;
3. keep the edit as narrow as possible;
4. run focused consumer regressions plus the normal repository gates;
5. do not obtain green by weakening the existing contract or oracle.

## Pre-human and accessibility truth

Parallel throughput does not relax release gates.

- `HUMAN_TESTED=false` until a person performs the protocol.
- `NVDA_VERIFIED=false` until a real NVDA test is performed.
- automated UIA/DOM/semantic tests are machine evidence only.
- Windows/NVDA interaction priority remains native/app API, semantic DOM/UIA, named deterministic
  controls, vision/OCR fallback, coordinates last.
- high-impact actions remain inside their approval boundaries.

## Reconciliation snapshot — non-authoritative after any live change

The anti-staleness reconciliation started from:

- `main` `109829579ab4693e038e218769c23c2547defd64`;
- exact-base Core CI `32981968912`: SUCCESS;
- M11 `32981968937`: SUCCESS;
- M12 `32981968851`: SUCCESS;
- `main` protection observed disabled and repository rulesets empty.

During the same live read, many current production/QA lanes were already newer than the old
2026-08-20 board, and several reservations existed as branches before PR creation. That is the
reason this file no longer enumerates a supposedly current owner roster.

Do not use this snapshot to decide current ownership, mergeability, current CI state, or current
`main`.

## Integration protocol

Immediately before integration:

1. freeze and re-read the exact candidate head;
2. re-read live `main`;
3. verify current ancestry/mergeability and dependency ordering;
4. verify required exact-head Core and milestone-specific gates;
5. verify required independent QA/audit on the same exact source identity;
6. confirm no newer conflicting owner/contract landed;
7. preserve rollback/history and do not force-push accepted evidence away;
8. merge only through the repository's guarded integration process.

A successful historical workflow run is lineage evidence only after the source head or relevant
base/contract changes.

## Maintenance rule for this board

Do not manually copy the current PR list into this file. That design caused the 2026-08-20 board to
remain frozen while live development advanced by hundreds of issue/PR numbers and multiple main
merges. Keep durable collision/integration rules here; keep volatile owners and exact evidence in
live GitHub.
