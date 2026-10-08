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


## Host authority admission (Plan 6 §1, component-only)

When an ASGI Host header is supplied, it must match an exact configured HTTPS
origin authority (case, port and IPv6 spelling included); when Origin is also
supplied, the two authorities must match. Rejection occurs before principal
resolution, JSON parsing and all Core effects, including for forged hosts,
malformed bytes and wrong ports. Explicit non-browser requests without Origin
remain supported only when their supplied Host belongs to the allowlist and
the server has separately established WebPrincipal.

This is strict ingress routing, not tenant account membership, an authentication
service or deployment reverse-proxy verification. The server remains responsible
for authenticating identities, controlling proxy headers, validating workspace
ownership and ensuring HTTPS termination. Tests include forged Host/no-effect
and valid Host paths; automated exact-head pass and upstream/main integration
are required before any Section 1 closure claim.


## ASGI principal read/retry chronology

After trusted server middleware sets the exact WebPrincipal in ASGI scope, the
ingress adapter immediately snapshots and revalidates its tenant, user,
workspace and session fields before awaiting body chunks. Any mutation to the
scope-owned carrier during async receive cannot retarget the Core effect.
Invalid previously mutated identity is rejected 401 before reading the body.
The existing WebApplicationBoundary remains the independent authorization
decision point; no new authentication or tenant-policy authority is introduced.


## Read-only SQLite query failure semantics

The shared Core TaskQueue remains the only task-state authority. If its
read-only inspection encounters sqlite3.Error, WebTaskQueryHandler emits a
bounded, secret-free `failed/storage_unavailable` result associated with the
original request_id. A database read failure cannot be described as an
outcome-unknown write or automatic retry permission. The handler does not
recreate or mutate failed state; a later valid query after canonical SQLite
reopen returns the unchanged Core record. Independent real-cloud storage
availability and worker recovery still require explicit qualification.

## Unknown-effect reconciliation identity (Plan 6 §1, component-only)

If a canonical application handler raises WebCommandOutcomeUnknownError after a
possibly applied effect, WebApplicationBoundary replaces its reported
correlation ID with the admitted request ID captured before handler dispatch.
This also holds when a handler mutates its disposable command carrier before
failing. The HTTP 409 response references only the original admitted request;
it never authorizes a blind retry. Regression tests cover both direct Web
boundary and HTTP projections. This is not a cloud recovery or terminal DONE
claim; the canonical task/runtime journal remains the reconciliation authority.

Authorization faults occur before canonical Core command effects and therefore
map to a sanitized pre-effect HTTP 500, not 409/outcome_unknown, even when a
broken authorization adapter itself raises WebCommandOutcomeUnknownError with
a forged correlation identity. Tests require zero handler calls and no internal
exception or forged request identity in the HTTP response.


## Pre-dispatch ASGI receive fault and recovery (Plan 6 §1)

The optional HTTP adapter treats an unexpected ASGI receive failure as a
**definite pre-effect transport failure**. It returns a bounded HTTP 500
`request_receive_failed` response and never projects raw exception text,
request fragments or local file paths. A partial body is discarded, with no
call to the canonical Core handler. A later fully valid request may be
admitted normally; no local Web-side retry is scheduled. ASGI cancellation
(`asyncio.CancelledError`) is not suppressed, preserving host cancellation
semantics and preventing success from being reported for a cancelled request.

Regression: `tests/test_plan6_asgi_receive_fault_recovery.py` covers failures
before and after body fragmentation, multiple exception classes, zero effects,
secret-free responses, a subsequent healthy request and cancellation propagation.
These are source-level candidate tests until exact-head CI finishes; they are
**not** cloud-hosted performance, durability or terminal-Section evidence.


## Corrupt canonical task-row projection (Plan 6 §1, recovery fixture)

Web task inspection is read-only: a canonical TaskQueue deserialization TypeError
must be reported as `failed/storage_unavailable`, never an uncertain write.
JSON that parses to a scalar, list or null violates the existing TaskRecord
payload-object contract and must not be advertised as a valid state. Once the
record is loaded, foreign-workspace checks run *before* inspecting payload
shape, so a foreign malformed task and an absent task have the same response.

The new negative tests mutate only test SQLite rows, verify bounded redaction,
cross-workspace non-disclosure, and re-open corrected canonical SQLite data.
No new Web-owned state, retries or side-effect authority are introduced. The
source of truth is still TaskQueue. Invalid payloads that fail inside
TaskQueue.get before workspace membership is read still require deeper
end-to-end tenancy/privacy qualification; this patch is not terminal closure.


## Corrupt cross-workspace payloads fail closed (Plan 6 §1)

The canonical `TaskQueue.get` parses its SQLite payload before returning a
`TaskRecord`. If JSON decoding raises `ValueError` or a damaged carrier raises
`TypeError`, `WebTaskQueryHandler` checks *only* the task's workspace column
through the existing `TaskQueue.store` canonical SQLite connection. An absent
task or a row outside the authenticated WebPrincipal workspace receives the
same bounded `not_found` result. Only a damaged task in the requesting
workspace receives `failed/storage_unavailable`. A failed metadata read also
fails closed and never reflects storage content.

The handler neither repairs corrupted payloads nor invents Web-owned task
state, mutation retries or authorization. The server-side authorization port
must independently enforce real tenant membership and entitlements. The
regression covers syntactically corrupt foreign JSON, body parity with missing
tasks, legitimate local failure, correction and canonical SQLite reopen. No
claim of hosted multi-tenant qualification or terminal DONE follows.

## Deeply recursive corrupt canonical payloads (Plan 6 §1)

A stored TaskQueue payload that raises `RecursionError` during canonical
`TaskQueue.get` JSON decoding is a **definite read failure**. It is handled
through the existing workspace-ownership metadata fallback: a foreign row
looks identical to a missing row, while the owning workspace receives bounded
`failed/storage_unavailable`. Neither user data nor parser details are
reflected, no Web-owned store/retry authority is introduced, and corrected
SQLite data remains readable after store re-open.

`tests/test_plan6_deep_corrupt_task_recovery.py` exercises deep nested JSON,
foreign/missing response parity, owner error classification, no secrets,
canonical data correction and restart-style readback. This component repair
does **not** satisfy outstanding server authentication, tenant/entitlements,
durable cloud execution, exact-head CI/main integration or Section 1 terminal
DONE by itself.

## Foreign-task payload admission before canonical JSON decoding (Plan 6 §1)

The read-only Web projection now checks only the existing Core SQLiteStore
`tasks.workspace_id` using the supplied task ID **after** the independent
WebAuthorizationPort decision and **before** `TaskQueue.get` deserializes
persisted payload JSON. Missing and foreign records both return the same opaque
`not_found`, without parsing large/deep/damaged foreign task payloads.
This avoids attacker-triggerable parse cost for data outside the caller's
workspace. A legitimate owner continues through the canonical TaskQueue;
there is no Web database, duplicate permission service or alternative runtime.

This preliminary read is not a lock or tenant authentication. If the row is
transferred while the canonical read is in flight, the resulting TaskRecord is
still checked against the principal. If parsing fails during the transfer,
the existing metadata-only fallback checks current workspace identity again
before exposing a bounded storage error. SQLite faults remain definite
`storage_unavailable` without exception details. The canonical Core store
can be reopened and successfully queried without any Web recovery state.

Focused evidence: `tests/test_plan6_task_preflight_isolation.py` covers
foreign/missing no-decode parity, owned canonical read and reopened SQLite,
secret-free storage failures and clean recovery, a healthy cross-workspace
transfer race, an invalid JSON transfer race and an owned deeply nested
payload failure. Tests are authored and require exact-head CI execution.
This component does **not** establish server-account authentication,
commercial entitlements, durable cloud workers, multi-tenant production,
Windows package proof, or terminal Section 1/2 DONE.
