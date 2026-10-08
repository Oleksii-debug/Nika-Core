# Plan 6 / Section 2 — accessible Web inspection client (nonterminal)

Canonical Drive plan: `6. Шостий план`, Section 2 (legacy Section 41).

## Existing-source reuse

The existing Windows HTML shell in `src/nika_core/ui/web` provides the
semantic headings, form/label, focus and live-status interaction conventions.
This separately packaged browser presentation is intentionally small, avoiding
Windows-local settings, filesystem paths, WebView2 bridge assumptions or a
second Core runtime. Command dispatch is exclusively to the existing Plan 6
Section 1 `ASGICommandApplication` through `/v1/commands`.

## Exact component behavior

- `AccessibleWebClientApplication` serves `/app/`, `/app/client.js` and
  `/app/styles.css` over HTTPS only, for an exact configured trusted Host.
  No directory traversal, dynamic template values or unbounded static files.
- Public static responses have no account-specific data and send CSP
  `default-src 'none'`, same-origin scripts/styles/connect, no forms/frames,
  `nosniff`, `no-store`, and `no-referrer`. Files are included in the wheel
  by setuptools package-data configuration.
- The client uses a labeled task ID field and explicit submit button, a skip
  link, main landmark and polite text status. Results use `textContent`;
  responses are constrained to `task.inspect` and display only a task ID and
  canonical state after verifying the returned ID. No autofetch/auto-retry.
- API calls use same-origin HTTPS, `credentials: "omit"`, no stored bearer
  tokens, no tenant/workspace/entitlement fields supplied by browser code.
  The host server must establish WebPrincipal and authorize each request.
  With no established server identity, the existing API returns 401.
- Failed/network/aborted requests produce text errors, clear stale state and
  remain manually recoverable. The browser cannot assume that a server task
  write did not occur; this component submits only read-only inspections.
- Tests cover static security headers, hostile/duplicate/missing Hosts,
  downgrades, unknown routes, inaccessible methods, lack of Core effects,
  component semantics, and no-host-principal API delegation.

## Explicit limits / terminal gates

This is a **read-only accessible foundation**, not the commercial multi-tenant
account product. No server-owned login/session issuance, real tenant membership,
entitlement/billing, quota, usage plans, user workspace selection, broader
command journeys, browser real-screen-reader testing, hosted Cloud deployment
or release-ready proof is supplied by this candidate. Human browser NVDA
qualification and whole-product hosted acceptance are distinct Plan 7 evidence.
Do not grant permissions based on assets or static source tests.

Section 2 remains ACTIONABLE, **not terminal DONE** until its remaining old
Section 41 scope, secure deployed composition, exact-head dual-OS tests,
integration/readback and proper GitHub/Drive closure pass. This child PR
depends on the Section 1 lineage; do not merge it before the parent is
qualified and integrated into canonical `main`.


## Stalled response-stream recovery — Plan 6 §2

When the 15-second browser request deadline aborts, an untrusted response
stream may still leave `reader.read()` unresolved. The existing bounded
JSON reader now races each read against that same abort signal, detaches its
abort listener after completion, and never awaits a misbehaving
`reader.cancel()` during cleanup. The form's busy state and disabled submit
button therefore become recoverable even if stream cancellation is unresolved.
The safety contract remains same-origin, read-only, manual retry, no browser
authority, no storage of credentials and output-only `textContent`.

`tests/test_plan6_web_client_response_runtime.py` adds the negative
`stalled-stream` case with both unresolved reader and unresolved cancellation,
plus prior happy-path/correlation/response-budget tests. Focused exact-source
V8 in-process checks exercised success, stalled-stream abort, wrong MIME and
edited task ID; these are **not** a checked-in Node run, real browser/NVDA
proof, full dual-OS CI, commercial account layer, or terminal DONE.
