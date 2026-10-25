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
15. Every durable assignment identity is re-derived from `(project_id, role_id, generation)` on
    restore. Candidate/corrupt state cannot substitute a generation-zero assignment ID or rewrite
    a complete predecessor/replacement ID chain while remaining internally self-consistent.

## Independent audit repair — AUD01 / AUD03 identity forgery

AUD01 reviewed exact head `3641801ea024711b000050f4987ff654baeb5351` and classified it
`BLOCK`: `_assignment_id()` was deterministic when Nika created an assignment, but restart
validation accepted any non-empty unique candidate-supplied `assignment_id` so long as the
predecessor chain was internally consistent. QA-only PRs #171/#187 captured the same adversarial
family.

MANUAL-DEV03 repaired the owned production slice without weakening or importing the QA vehicle:

- `_validate_snapshot()` now re-derives the expected assignment ID from the durable project ID,
  role ID and generation and rejects any mismatch;
- the DEV03 regression suite now includes the exact generation-zero substitution attack;
- the DEV03 regression suite also consistently rewrites generation-zero and generation-one IDs
  plus `replaces_assignment_id`, proving internal self-consistency cannot bypass canonical ID
  derivation;
- legitimate generation/replacement round trips remain covered by existing positive tests.

Independent auditor replay/reclassification is still required before integration credit. DEV03
does not self-certify the prior auditor BLOCK as cleared.

## Scale evidence

The lifecycle test matrix exercises 1, 5, 25, and 100 component requests. Assertions prove
component coverage, deterministic role semantics, permission bounds, and independent review.
Large-project checks prove deterministic implementation fan-out by component; they do not treat
raw agent/role count as a quality metric.

Existing repository scale tests remain the authoritative broader Product Factory proof for
coordinator scheduling and checkpoint recovery. This PR does not duplicate or weaken those gates.

## Validation truth

Superseded head `3641801ea024711b000050f4987ff654baeb5351` had Core CI #1082
terminal `SUCCESS`. M12 #850 passed complete source/recovery verification on both platforms and
passed the Windows semantic interaction proof, but the Ubuntu job failed in the unrelated browser
semantic-interaction re-proof; therefore M12 was `FAILURE` and received no acceptance credit.

That head is additionally invalidated by the later assignment-identity repair. Earlier local
harness counts and pre-repair CI are lineage/diagnostic evidence only. Only fresh repository
GitHub Actions on the post-repair exact PR head count for merge readiness.

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
