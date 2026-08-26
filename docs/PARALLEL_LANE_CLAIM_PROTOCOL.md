# Parallel lane claim protocol

Status: ACTIVE DEVELOPMENT COORDINATION CONTRACT

## Purpose

Nika Core is developed by many independent human and agent workers. Parallel work is useful only when
two workers cannot silently take authority over the same mutable slice. GitHub live state is the
coordination authority; chat memory and old status prose are not.

This protocol standardizes a small, visible, machine-checkable lane claim. It does **not** create a
scheduler, distributed lock service, GitHub security boundary, or product-runtime dependency.

## Canonical live surfaces

Before any write, a worker must reread:

1. exact live `main` SHA;
2. open pull requests and their exact heads/diffs;
3. GitHub Issue #1 current coordination comments;
4. relevant current Drive handoff/ownership record when one exists;
5. the specific files/contracts that the proposed lane will edit.

An open PR with a current exact owner remains authoritative even if an older claim expired. A claim
never grants permission to overwrite an existing owner.

## Claim-before-write sequence

For a new mutable lane:

1. choose the narrowest coherent file/path scope that can finish useful work;
2. race-check current `main`, open PRs and Issue #1 immediately before claiming;
3. publish a visible Issue #1 comment containing a human-readable heading and one exact
   `NIKA_LANE_CLAIM_V1=<json>` line;
4. create a unique feature branch from the recorded `start_main`;
5. open a draft PR as soon as the first coherent commit exists;
6. before each later shared-file write, recheck live ownership and stop on any overlap;
7. publish a newer `released` record when the lane is handed off, merged, abandoned or superseded.

If Issue-comment creation is temporarily unavailable, a uniquely named `work/<lane>/...` branch may
serve as a **provisional** public claim for additive, previously nonexistent paths. Do not edit a
pre-existing shared file until either the Issue #1 claim or a draft PR makes the intended shared-file
scope explicit.

## Version 1 payload

Example:

```text
NIKA_LANE_CLAIM_V1={"schema":"nika-lane-claim/v1","lane_id":"W123","owner":"worker-name","status":"active","start_main":"0123456789abcdef0123456789abcdef01234567","branch":"work/w123/example","scope":["scripts/example.py","tests/test_example.py","docs/EXAMPLE.md","src/nika_core/example/**"],"created_at":"2026-08-26T20:00:00Z","expires_at":"2026-08-27T20:00:00Z","pr":null}
```

Fields:

- `schema`: exactly `nika-lane-claim/v1`;
- `lane_id`: stable uppercase lane identity;
- `owner`: human-readable worker identity;
- `status`: `active` or `released`;
- `start_main`: exact lowercase 40-hex `main` SHA observed at claim time;
- `branch`: unique Git branch;
- `scope`: non-empty repository-relative exact paths and/or trailing `/**` prefixes;
- `created_at`: canonical UTC timestamp ending in `Z`;
- `expires_at`: canonical UTC timestamp ending in `Z`, at most 24 hours later;
- `pr`: positive PR number when known, otherwise `null`.

A newer record for the same `lane_id` supersedes an older one. A `released` record closes the lane.
Equal-time conflicting records fail closed.

## Scope collision semantics

Scopes are lexical repository paths, not filesystem containment proofs.

- `path/file.py` claims exactly that path.
- `path/subtree/**` claims the subtree root and every descendant.
- exact paths collide only when equal.
- a prefix collides with an exact path inside that prefix.
- two prefixes collide when either contains the other.
- sibling prefixes do not collide.
- absolute paths, backslashes, traversal, `.git`, embedded wildcards and non-canonical path forms are
  rejected.

A path claim is only a collision detector. Shared-contract changes can require a compatibility
decision even when filenames differ.

## Lease lifetime and stale claims

Version 1 limits a claim to 24 hours so an abandoned chat cannot block development indefinitely.
Workers doing longer work publish a fresh claim after another full live-state race check.

Expiry does not erase GitHub truth. An open PR, newer Issue #1 owner record, explicit integration
dependency, or current Drive handoff can still prove that a slice is occupied.

## Deterministic validator

Use only the Python standard library:

```text
python scripts/nika_lane_claim.py validate claim.json --now 2026-08-26T21:00:00Z
python scripts/nika_lane_claim.py check claim.json --against other-claims.json --now 2026-08-26T21:00:00Z
```

Input may be one JSON object, a JSON array of claim objects, or text containing exact
`NIKA_LANE_CLAIM_V1=` lines copied/exported from coordination comments.

Exit codes:

- `0`: valid / no collision;
- `2`: malformed or ambiguous claim input;
- `3`: one or more active collisions.

The validator intentionally performs no network access, GitHub mutation, secret access, branch
creation or merge. It can be used in a worker sandbox and remains deterministic.

## Security and authority boundary

This is coordination evidence, not authentication. Payload text cannot prove who authored a GitHub
comment and cannot override repository permissions, branch protection, review requirements,
current code ownership or release gates.

A worker must not use a syntactically valid claim to:

- take over another active PR;
- edit another lane's files without a compatibility decision;
- write directly to `main`;
- bypass CI/review/audit;
- expand permissions;
- claim HUMAN_TESTED or NVDA_VERIFIED.

## Required handoff truth

A lane handoff records at minimum:

- starting and final race-check `main` SHA;
- branch and exact head SHA;
- PR number/state;
- exact changed paths;
- tests actually executed and their result;
- known conflicts/dependencies;
- what is not verified;
- `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` unless real human evidence exists.

This protocol reduces accidental duplicate work; it does not replace dependency-aware integration.
