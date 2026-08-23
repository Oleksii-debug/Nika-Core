# MANUAL-DEV03 PF2 Dynamic Team Lifecycle Evidence — 2026-08-23

## Canonical baseline

- repository: `Oleksii-debug/Nika-Core`
- starting live `main`: `bd7517f38c04560aa7350b870d8a51bfb6c8113b`
- live `main` after in-run synchronization: `e40691a6e2ff9c31fd413f63d004612e048d95ed`
- synchronization commit: `1bda5761e59cb9687abd4ee5f3acd6ba9b05ee8e`
- lane: `MANUAL-DEV03`
- gate: `PF2`
- branch: `work/manual-dev03/pf2-team-lifecycle`
- HUMAN_TESTED: `false`
- NVDA_VERIFIED: `false`

The run re-read the binding Product Factory specification, acceptance gate, project state,
parallel execution board, Issue #1 control stream, open PRs, and current ownership before writing.
GitHub live state was treated as authoritative over stale state-document snapshots.

## Collision and synchronization decision

At run start PR #160 owned `src/nika_core/product_factory_orchestration.py`, including active PF2
lease-identity work. MANUAL-DEV03 therefore did not edit that file or its lease tests.

During this run PR #160 merged, advancing live `main` from `bd7517f...` to `e40691a6...`.
The MANUAL-DEV03 branch was then synchronized without force-push using a two-parent commit whose
tree is current main plus only the MANUAL-DEV03-owned files. No stale pre-merge version of
`product_factory_orchestration.py` was overlaid onto the merged lease-identity fix.

This slice is deliberately isolated in:

- `src/nika_core/product_factory_team_lifecycle.py`
- `tests/test_product_factory_team_lifecycle.py`
- `tests/test_product_factory_team_lifecycle_audit.py`
- this evidence document

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
14. Replacing an unavailable worker does not overwrite the original blocked/failed reason or
    evidence. The terminal historical assignment retains unavailable evidence while the new
    assignment carries replacement-decision evidence, and the separation survives restart.

## Scale evidence

The lifecycle test matrix exercises 1, 5, 25, and 100 component requests. Assertions prove
component coverage, deterministic role semantics, permission bounds, and independent review.
Large-project checks prove deterministic implementation fan-out by component; they do not treat
raw agent/role count as a quality metric.

Existing repository scale tests remain the authoritative broader Product Factory proof for
coordinator scheduling and checkpoint recovery. This PR does not duplicate or weaken those gates.

## Validation truth

Before the final audit-evidence defect was found, a live-signature-compatible local harness had
reported `19 passed`, and a superseded exact-head Core run had already proved dependency
consistency, Ruff, and compile before its full pytest run was cancelled by a newer push at 68%
with no failures observed to that point.

Those results are lineage/diagnostic evidence only. The subsequent audit fix changed production
source and added two regression tests, so no local success count is claimed for the final source.
Only repository GitHub Actions on the final exact PR head count for merge readiness.

## DEPENDENCY_REQUEST

Canonical durable storage of this lifecycle payload belongs at the ProductProject / coordinator
persistence boundary. That source slice is outside MANUAL-DEV03 ownership and must not be edited
without a compatibility decision from its active owner / TECH02.

Requested integration decision:

1. choose the canonical ProductProject field/checkpoint location for `TeamLifecycleSnapshot`;
2. persist the versioned JSON transactionally with the owning ProductProject revision/checkpoint;
3. restore through `TeamLifecycleSnapshot.from_json()` so corruption fails closed;
4. wire coordinator worker-unavailable events to `mark_unavailable()` and
   `replace_unavailable()` without creating a second competing ownership model;
5. retain permission attenuation at every reactivation/recomposition boundary.

Integration and merge ownership remains TECH02. This lane does not self-merge.
