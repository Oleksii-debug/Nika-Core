# Parallel lane claims

Status: developer-infrastructure contract. This is advisory coordination, not product or security authority.

## Why this exists

Nika Core already uses GitHub Issue #1 as the live coordination and handoff surface, but parallel workers currently interpret free-form markers themselves. A worker can also lose the ability to publish a marker when GitHub applies a secondary content-write rate limit. That must fail closed for ownership rather than silently becoming permission to edit a possibly occupied surface.

`scripts/nika_lane_claim.py` reuses Issue #1 and the existing `NIKA-C10` marker convention. It adds deterministic parsing and conflict resolution; it does not add a database, service, GitHub App, workflow, credential store, or product runtime authority.

## Required preflight

This helper is one input to the normal live reread. It never replaces checks of live `main`, open PRs, Actions, Issue #1 handoffs, Drive routing, or the canonical owner of an existing production PR.

Example read-only check:

```text
python scripts/nika_lane_claim.py check --claim-key coordination/lease \
  --path scripts/nika_lane_claim.py --path tests/test_nika_lane_claim.py
```

Exit code `0` means the helper found no active overlapping Issue #1 claim. Exit code `2` means an active conflict exists. Exit code `3` means the GitHub channel could not be read or used safely. A zero result is not permission to take an existing PR owner's files.

To publish a bounded advisory claim, provide `GH_TOKEN` or `GITHUB_TOKEN` only through the environment and run:

```text
python scripts/nika_lane_claim.py claim --worker-id C10-EXAMPLE \
  --run-id C10-EXAMPLE-20260826T2315+0300 --claim-key coordination/lease \
  --lease-minutes 360 --path scripts/nika_lane_claim.py \
  --path tests/test_nika_lane_claim.py --path docs/PARALLEL_LANE_CLAIMS.md
```

The token is sent only in the HTTPS Authorization header. It is never printed by the helper. Do not put a token in command arguments, source, a claim body, a log, or Git.

Release the same run explicitly:

```text
python scripts/nika_lane_claim.py release \
  --run-id C10-EXAMPLE-20260826T2315+0300 --claim-key coordination/lease
```

## Deterministic conflict rule

A version-1 claim contains a run ID, claim key, lease duration, and normalized ownership paths. The lease is bounded to 5-720 minutes.

For active claims, conflict exists when either:

- the claim key is identical; or
- one normalized ownership path equals, contains, or is contained by another.

Claims are considered in authoritative GitHub `created_at` order, then by numeric comment ID. The earliest active non-conflicting claim wins. A concurrent later claimant therefore loses deterministically after it re-reads Issue #1. A later release from the same GitHub actor for the same run ID and claim key ends that claim. Expired claims do not block.

The parser also recognizes recent legacy `[NIKA-C10:<lane>]` comments with active statuses and `OWNERSHIP_PATHS`. Legacy markers have no explicit lease, so the helper applies a bounded compatibility TTL (12 hours by default). This is only a migration aid; open PR ownership and newer handoffs still take precedence.

## Failure semantics

`claim` always performs:

1. live Issue #1 read;
2. conflict check;
3. claim comment write;
4. live Issue #1 re-read;
5. deterministic winner verification.

If any read/write fails, including HTTP 403 secondary rate limiting, the command returns exit code `3` and does not report ownership. If a concurrent earlier claim wins, the command returns `2` and the caller must not edit that surface.

Branch creation alone is useful evidence of intent but does not override a conflicting canonical owner. A worker must not convert an unavailable claim channel into implicit ownership.

## Security boundary

This helper is deliberately not an R0-R4 approval mechanism, lock service, merge authority, authentication layer, or permission ceiling. It cannot authorize production effects, repository writes, workflow mutation, merges, releases, credentials, deployments, or self-modification. GitHub authentication and the existing Nika acceptance/integration rules remain authoritative.

`HUMAN_TESTED=false` and `NVDA_VERIFIED=false` are unaffected by this developer tool.
