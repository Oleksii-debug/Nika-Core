# Universal Research Source Identity and Fetch Classification

Scope: canonical Universal Research HTTP/API source identity, provenance safety, incremental-fetch compatibility, and deterministic failure classification. Multi-format report export remains owned by DEV01 PR #86 (or its successor) and is intentionally not changed here.

## Source identity invariants

An HTTP `source_id` is a durable provenance identity, not a mutable bookmark.

- A `source_id` is permanently bound to the workspace in which it was first registered.
- A `source_id` is permanently bound to one canonical HTTP locator. To research a replacement locator, register a new `source_id`; do not rewrite historical identity.
- The canonical locator normalizes scheme/host case, IDNA host spelling, default ports, an empty path, and fragments. Query spelling and ordering are preserved because they may change the fetched resource.
- Two different `source_id` values in one workspace may not resolve to the same canonical locator.
- URL userinfo (`user:password@host`) and credential-bearing query parameters (for example API keys, access tokens, cloud signed-URL credential identifiers, signatures, and passwords) are rejected before SQLite persistence. Rejection messages do not echo the credential-bearing URL.
- Re-registering the same identity is idempotent and must not clear ETag, Last-Modified, raw-content hash, freshness, or prior provenance.
- If a persisted HTTP locator cannot be validated after restart, the repository reports `source_identity_corrupt` and fails closed. It does not return the corrupted locator, skip it during duplicate checks, or admit a new source into that workspace while source identity is unverifiable.

These rules deliberately avoid a new migration. The existing `(workspace_id, url)` uniqueness remains useful, while the repository performs canonical duplicate checks and immutable-identity checks before writes. This avoids a shared schema-version collision with parallel lanes.

## Credential-safe fetch and redirect handling

The same credential-free locator policy is applied before HTTP transport. A direct credential-bearing URL is blocked without returning the original secret-bearing value in `HttpFetchResult`; the public result uses a redacted marker instead.

Redirect targets are canonicalized and checked before a second request. If a `Location` target contains URL userinfo or a recognized credential-bearing query parameter, the fetch is blocked while `requested_url` and `final_url` remain at the last safe locator. The credential-bearing redirect target is therefore neither requested nor written to durable HTTP attempt history.

HTTPX cookie state is cleared before every request, and redirect responses do not become an implicit cookie-authentication channel. Credentials/cookies remain separate connector/policy concerns rather than source-locator state.

## Incremental fetch and evidence compatibility

The existing HTTP path remains authoritative:

`registered source -> conditional HTTP fetch -> ETag/Last-Modified -> raw SHA-256 -> unchanged suppression -> extraction -> corpus/evidence`

Identity hardening does not replace HTTPX, SQLite, content-addressed blobs, extraction, or result-set persistence. Because a source cannot be rebound after observations exist, persisted `ResearchEvidence(source_id, locator, observed_at, freshness)` cannot silently acquire a different owner or locator after restart. Product Factory consumers therefore continue to receive the existing research evidence contract without a second research engine or a DEV02-specific provenance model.

## Typed fetch failure classification

`HttpFetchResult.failure_class` is a stable provider-neutral classification layered on top of the existing `disposition` and `error_code` fields and is propagated by canonical `RefreshResult` for service consumers:

- `network` — retryable transport, DNS/socket, or timeout failures;
- `private` — a source/redirect resolving only to non-public addresses under the active policy;
- `auth` — HTTP 401/403 requiring credentials or access not granted to Research;
- `unsupported` — unsupported source scheme or response media type;
- `policy` — malformed/disallowed URL, host, port, userinfo/query credentials, or insecure-HTTP policy failure;
- `http` — non-success HTTP/redirect status semantics;
- `resource` — response-size/resource bound failure.

The field is additive and backward-compatible: existing disposition/error-code behavior is retained so current callers do not need a flag day. No credential, cookie, provider object, or HTTPX exception type is required in the domain handoff.

## REUSE / ADAPT / CUSTOM

- **REUSE** — maintained HTTPX transport, existing DNS pinning/redirect revalidation, SQLite repository, content-addressed blob store, corpus/evidence/result contracts.
- **ADAPT** — standard-library URL parsing/IDNA/query parsing is adapted only for source identity and credential-safe transport preflight.
- **CUSTOM (thin)** — Nika-specific immutable source identity errors, corrupt-state guard, credential-safe source/redirect boundary, and the small stable fetch-failure enum.
- **No new dependency and no migration.**

## Acceptance evidence

Network-free deterministic tests must prove canonical/idempotent registration, duplicate rejection, cross-workspace and locator mutation rejection across restart, credential non-persistence (userinfo/query/signed-URL credentials), fail-closed behavior for malformed or credential-bearing persisted locator state after restart, direct-fetch result redaction, redirect blocking before the second request, absence of redirect credentials from durable attempt history, distinct private/auth/unsupported/network failure classes, service-level typed failure handoff, and evidence locator stability after a rejected rebind.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`. This backend contract does not claim a human accessibility verification gate.
