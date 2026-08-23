# MANUAL-DEV03 PF2 Dynamic Team Lifecycle Evidence — 2026-08-23

## Canonical baseline

- repository: `Oleksii-debug/Nika-Core`
- starting live `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`
- lane: `MANUAL-DEV03`
- gate: `PF2`
- branch: `work/manual-dev03/pf2-team-lifecycle`
- HUMAN_TESTED: `false`
- NVDA_VERIFIED: `false`

The run re-read the binding Product Factory specification, acceptance gate, project state,
parallel execution board, Issue #1 control stream, open PRs, and current ownership before writing.
GitHub live state was treated as authoritative over stale state-document snapshots.

## Collision decision

Open PR #160 owns `src/nika_core/product_factory_orchestration.py`, including active PF2 lease
identity work. MANUAL-DEV03 therefore did not edit that file or its lease tests.

This slice is deliberately implemented as a new adapter module:

- `src/nika_core/product_factory_team_lifecycle.py`
- `tests/test_product_factory_team_lifecycle.py`

The adapter reuses the already-integrated `DynamicTeamComposer`, `TeamCompositionRequest`,
`TeamPlan`, and `TeamRole` contracts.

## REUSE → ADAPT → CUSTOM(thin)

- REUSE: integrated deterministic PF2 composer and role contracts.
- ADAPT: add deterministic role-assignment lifecycle, recomposition, restart serialization, and
  permission-ceiling enforcement around those contracts.
- CUSTOM(thin): Nika-specific lifecycle invariants only. No generic multi-agent framework and no
  new dependency were introduced.

## Invariants implemented

1. Same composition request produces byte-identical versioned lifecycle JSON.
2. Current logical roles are semantically deduplicated by capabilities, component ownership, and
   independent-review identity.
3. Replaying the same specialist addition is a no-op instead of producing a duplicate role.
4. Specialist permissions are always attenuated to the ProductProject permission ceiling.
5. Recomposition may narrow authority but cannot silently widen the existing ceiling.
6. Existing matching role/assignment identities survive additive recomposition.
7. Removed specializations are retired into history instead of silently deleted.
8. Only `blocked` or `failed` current assignments may be replaced.
9. Replacement changes assignment generation/identity while preserving the logical role,
   component ownership, capabilities, and permissions.
10. Replacement predecessor links are exact and generation-monotonic.
11. Restart payloads are schema-versioned and reject unknown keys, unknown schema versions,
    duplicate current semantics, permission escape, and broken predecessor identities.
12. Historical replaced/retired assignments remain audit records when the current permission
    ceiling narrows; only current executable assignments must fit the current ceiling.
13. Historical roles can only become executable again through deterministic recomposition, which
    re-applies the current permission ceiling.

## Scale evidence

The lifecycle test matrix exercises 1, 5, 25, and 100 component requests. Assertions prove
component coverage, deterministic role semantics, permission bounds, and independent review.
Large-project checks prove deterministic implementation fan-out by component; they do not treat
raw agent/role count as a quality metric.

Existing repository scale tests remain the authoritative broader Product Factory proof for
coordinator scheduling and checkpoint recovery. This PR does not duplicate or weaken those gates.

## Cheap validation before final push

The source and test files were syntax-compiled and exercised through a live-signature-compatible
contract harness. The final pre-CI harness result after the history/ceiling edge-case fix was:

- `19 passed`

This harness is not acceptance evidence. Repository GitHub Actions on the exact PR head are
required before merge readiness can be claimed.

## DEPENDENCY_REQUEST

Canonical durable storage of this lifecycle payload belongs at the ProductProject / coordinator
persistence boundary. That source slice overlaps active ownership and must not be edited by this
lane without a compatibility decision.

Requested integration decision after PR #160 / relevant ProductProject ownership clears:

1. choose the canonical ProductProject field/checkpoint location for `TeamLifecycleSnapshot`;
2. persist the versioned JSON transactionally with the owning ProductProject revision/checkpoint;
3. restore through `TeamLifecycleSnapshot.from_json()` so corruption fails closed;
4. wire coordinator worker-unavailable events to `mark_unavailable()` and
   `replace_unavailable()` without creating a second competing ownership model;
5. retain permission attenuation at every reactivation/recomposition boundary.

Integration and merge ownership remains TECH02. This lane does not self-merge.
