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

## Converged ingress framing invariants (Plan 6 §1, not terminal DONE)

This successor combines the canonical Origin/header validation in PR #1751
with the independent Content-Length and duplicate-sensitive-header repair in
PR #1744. The server rejects duplicate Host/Authorization/Content-Length/
Content-Type/Cookie/Origin/Transfer-Encoding headers case-insensitively;
contradictory Content-Length and Transfer-Encoding; non-decimal, oversized or
received-length-mismatched Content-Length. Reject these before command effects.
Request bodies remain bounded to 256 KiB. Tests cover malformed and valid
fragmented requests plus no-effect rejection.

These controls do not establish server authentication, tenant ownership,
durable cloud work, commercial entitlements or Section 1 terminal closure.

## Read-only shared Core task readback

The Web `task.inspect` projection delegates directly to the incumbent
`TaskQueue.get` and returns only durable task ID and canonical state. It does
not expose the stored payload, agent configuration, secrets or unrelated
workspace records. A missing record and a record in another workspace have
the same response. After SQLiteStore re-open, the same scoped query uses the
existing persisted task state, not a new Web snapshot or scheduler.

Authentication, server-owned tenant/workspace entitlement and API mounting
remain requirements of the composition root. `WebTaskQueryHandler` must be
passed behind a server-authorized `WebApplicationBoundary`; workspace equality
alone is not an account/tenant ownership proof. This read-only integration
does not grant task creation, cloud worker dispatch, provider credentials or
any production-ready Web/Cloud claim.

## Permission-decision carrier isolation (Plan 6 §1, component-only)

The trusted server principal and admitted command are copied before the
replaceable WebAuthorizationPort is called. The authorization adapter receives
separate disposable dataclass carriers. Even if an adapter improperly mutates
a frozen principal or command through `object.__setattr__`, the Core handler
receives the previously admitted tenant/user/workspace/session and command.
The caller-owned principal is revalidated, and outcome-unknown reconciliation
uses the request ID captured before the handler call.

Regression tests cover authorization-carrier mutation, mutation of the
caller-owned principal during authorization, invalid post-construction
principal identity, and a failing handler that changes its command carrier.
These are committed regression cases, not an asserted executed pytest pass;
exact-head CI, authenticated server composition, tenant ownership,
durable cloud lifecycle and main integration still govern terminal closure.
