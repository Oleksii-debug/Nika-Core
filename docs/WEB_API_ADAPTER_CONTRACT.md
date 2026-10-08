# Plan 6 / Section 1 — Web ingress component contract (nonterminal)

Canonical plan: Nika Core Drive "6. Шостий план", Section 1 (legacy Section 40).
State: IMPLEMENTED CANDIDATE, not terminal DONE.

## Adopted upstream project source
- The Nika-owned Web application boundary is reused from existing PR #1209 and
  HTTP transport from its dependent PR #1213, also present in #1248's terminal branch.
- The current main did **not** contain `src/nika_core/web_api`; this branch
  imports exact donor blobs rather than creating a second command/policy runtime.
- `WebCommand` / `WebPrincipal` / `HttpCommandAdapter` are projection and
  admission contracts; they do **not** themselves grant cloud execution authority.
- The ASGI edge is a thin optional adapter over `HttpCommandAdapter`.

## Required external composition
A trusted server authentication middleware must verify a real account/session
and place an **exact** `WebPrincipal` in
`scope["state"]["nika_principal"]`. The browser cannot set this scope member
by adding headers or JSON keys. Neither an Origin, Authorization nor Cookie
header is sufficient proof by itself. The application authorization port must
independently check workspace ownership, entitlements, permissions and budgets
against server-owned authority before forwarding to canonical task/agent
application services.

Mount the HTTP-only ASGI adapter at `/v1/commands`, behind authenticated HTTPS
termination with correctly configured trusted ASGI `scheme="https"`. Supply a
non-empty exact `frozenset` of allowed HTTPS Origins; do not supply a wildcard
or derive it from caller headers. No cookies are admitted because session-bound
CSRF semantics are not yet implemented; use authenticated server middleware
and request credentials that have been explicitly verified by the host. Do not
enable public ingress without that middleware and a real authorization port.

## Failure, security and performance
- Fail closed before effects for HTTP downgrades, unknown routes, unsafe Origins,
  cookies, missing server principal, duplicate sensitive headers and bad streams.
- Configuration rejects malformed HTTPS origin authorities (userinfo, paths, queries,
  fragments, invalid ports, noncanonical domain/IPv6 spellings and controls).
- The HTTP edge admits at most 64 headers and 16 KiB of header name/value bytes;
  malformed header names, CR/LF/NUL in values and larger header sets are rejected
  before the canonical application handler. Header abuse tests assert zero effects.
- Enforce the existing 256 KiB bounded JSON body and 1,024 receive-event limit;
  no unlimited request buffering or optional untrusted schema coercion.
- Pass approved envelopes to the existing detached Web command boundary.
- Handler uncertainty maps to the incumbent 409/reconciliation-required result;
  the ASGI edge does not retry, schedule or resume anything.
- Request outcomes are data-only UTF-8 JSON with `no-store`/`nosniff`;
  failure exceptions do not disclose secrets or user-provided raw material.
- Abort pre-dispatch on ASGI disconnect. Do not interpret disconnection during an
  already running handler as rollback; reconcile using canonical durable state.
- This component adds no new Python dependency, broker, scheduler, runtime,
  SQLite table, entitlement service, payment handler or Windows UI authority.

## Acceptance / remaining required work
Focused tests:
`tests/test_web_application_boundary.py`,
`tests/test_web_http_transport.py`,
`tests/test_web_asgi_transport.py`.
Dual-OS exact-HEAD Core CI must be evaluated; donor historical green is not
evidence for this branch. M12 is separate packaging/integrity evidence.

Section 1 remains open until an actual authenticated HTTP composition root
routes real core commands/queries, cloud-hosted durable work uses the incumbent
runtime/session/idempotency/recovery authorities, and exact-head security,
recovery, load/failure, multi-tenant and integration checks succeed and main
readback is complete.

Section 2 (accessible browser product/account/tenant/entitlement layer) is
not implemented by this infrastructure candidate; its own tests and
integration cannot be inferred. Full real Web/Cloud deployment and human
browser/NVDA evidence belong to Plan 7. Do not record terminal DONE prematurely.
