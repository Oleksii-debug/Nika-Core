# AI Trader DEV26 — strategy fingerprint framing integrity

Lane: `MANUAL-DEV26`.

## Defect

The first strategy-artifact fingerprint schema framed fields with a literal `|` delimiter while
`strategy_id` and `strategy_version` legitimately allowed that character. Two distinct artifacts
could therefore produce the same pre-hash bytes without any SHA-256 collision.

Concrete ambiguity:

- artifact A: `strategy_id="alpha|beta"`, `strategy_version="gamma"`;
- artifact B: `strategy_id="alpha"`, `strategy_version="beta|gamma"`.

With all remaining fields equal, v1 serialized both tuples identically. Because candidate/result
seals retain the strategy artifact fingerprint, a low-level post-construction replacement could use
that framing ambiguity to preserve the stored digest while changing semantic strategy identity.

## Repair

`StrategyArtifactFingerprint` now uses schema `nika-trader-strategy-artifact-v2` and hashes a
canonical compact JSON array instead of delimiter-joined text. JSON framing preserves field
boundaries and preserves `seed` as an integer value. UTF-8 identities remain supported, including
literal `|`; the repair does not narrow otherwise valid strategy identifiers merely to protect the
framing format.

SHA-256 remains the digest primitive. The defect was ambiguous serialization before hashing, not a
cryptographic collision in SHA-256.

## Regression evidence

The owner regression:

1. reconstructs the exact v1 framing and proves the two tuples above had equal legacy digests;
2. requires their v2 fingerprints to differ;
3. seals a candidate with one artifact, replaces it with the legacy-colliding artifact through a
   low-level mutation, and requires selection validation to fail closed.

Existing held-out seals, refit rules, dataset/metric/universe identity, data-quality evidence and
promotion chronology remain unchanged except that they now transitively bind the unambiguous v2
strategy artifact digest.

## Boundary

This change adds no dependency, broker, network order route, funding path, permission, approval or
real-money authority. Replay execution, accounting, risk and durable paper-session ownership remain
with active Trader PR #67 until that owner integrates or releases those paths.

`REAL_MONEY_AUTHORITY=false`.
`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
`PRODUCTION_RELEASE_READY=false`.
