# M10 R4 trusted human approval channel

Updated: 2026-08-19.

Status: stacked implementation candidate on top of PR #61. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.

## Purpose

R4 actions are actions for which Nika must not treat an agent-generated data structure as human consent. Examples include real-money, legal, destructive and privacy-sensitive operations. The downstream security boundary therefore needs two separate properties:

1. exact action identity: the approval must bind to the precise tool, target, path/network/process parameters, risk and approval mode;
2. trusted provenance: the evidence must have been issued by the packaged host after a person explicitly approved a pending request.

PR #61 hardens exact action fingerprints and atomic approval/budget accounting. This slice adds trusted provenance and an accessible desktop approval path.

## REUSE / ADAPT / CUSTOM

REUSE:

- Python `secrets.token_bytes(32)` and `token_urlsafe()` for process-ephemeral authority material and unguessable request/evidence IDs;
- Python `hmac` with SHA-256 and `hmac.compare_digest()` for integrity/authenticity of process-issued approval evidence;
- existing versioned `ActionIntent.approval_fingerprint`, `ApprovalLedger`, `SecurityPolicy`, pywebview bridge, Action Registry and semantic HTML controls;
- native HTML `<button>` controls rather than a custom JavaScript widget, preserving normal keyboard and accessibility semantics.

ADAPT:

- `SecurityPolicy` accepts a host-owned `ApprovalVerifier`; any approval-gated action fails closed when no trusted verifier exists;
- Toolsmith→M10 bridge may receive a verifier from its trusted host but never creates an approval authority for a runtime agent;
- the packaged Windows composition root creates one process-local `ApprovalAuthority`, passes it to the desktop backend and registers explicit-only approval actions;
- desktop snapshots expose safe pending request views, not signed evidence.

CUSTOM thin:

- a bounded pending-approval queue and HMAC evidence format `nika-r4-approval-evidence-v1`;
- request-ID-only approve/deny handlers binding the browser UI back to the exact Python-owned `ActionIntent`.

No external cryptography package, secret store or web service is introduced.

## Trust boundary

The packaged Python desktop process is trusted to own the approval authority. Its secret is generated at startup, is not persisted, is not committed and is never returned through the pywebview API.

The JavaScript UI receives only a view containing:

- `request_id`;
- action/tool/risk/target;
- reason shown to the person;
- exact write path/byte count, network host and executable when present;
- request and expiry timestamps.

The UI returns only `request_id` when the person activates Approve or Deny. It never returns the action parameters themselves. Therefore changing DOM text or adding payload fields cannot alter what the Python authority signs: the authority signs the original pending `ActionIntent` stored under that request ID.

A runtime agent must not receive the authority, HMAC key or verifier object. A downstream adapter receives only a `SecurityPolicy` whose verifier was injected by the trusted composition root. If arbitrary untrusted code can execute inside the trusted desktop Python process, this trust model is already broken; R4 is not claimed to defend against compromise of the host process itself.

## Evidence format

`ApprovalEvidence` contains:

- random `approval_id`;
- random `request_id`;
- authority `issuer_id`;
- exact `action_fingerprint`;
- `approved_at` and `expires_at`;
- HMAC-SHA256 signature.

The HMAC covers a compact ASCII JSON array with schema tag, every evidence identity field, exact action fingerprint and UTC timestamps. `hmac.compare_digest()` is used for signature comparison.

`authorize_action()` requires a trusted verifier for every high-impact or explicitly approval-required intent. Signature/issuer/exact-action/validity verification happens before the low-level one-time ledger commits approval use. The ledger uses `(issuer_id, approval_id)` as the one-time key.

Direct `ApprovalLedger.consume()` remains a low-level accounting primitive and is **not** a provenance boundary. Callers performing side effects must use `authorize_action()` with a trusted `SecurityPolicy`.

## Lifecycle and restart behavior

Pending approval requests default to five minutes and may never exceed fifteen minutes. Expired requests are pruned and cannot later be approved. Denied requests cannot later be approved. Approved evidence expires at the original pending-request deadline.

The default authority key is process-ephemeral. Restarting Nika invalidates previously issued approval evidence. This is intentional fail-closed behavior until a future durable approval design has an OS-backed secret/key store and a transactional recovery contract.

No `.env`, token, cookie, session or approval key is written to the repository or application database by this slice.

## Accessibility contract

The local web shell adds a semantic section headed “Підтвердження небезпечних дій”. Each pending action renders its exact reviewable parameters as text plus two native focusable buttons:

- “Підтвердити цю точну дію”;
- “Відхилити”.

Buttons receive accessible labels containing action ID and target. Pending state has an `aria-live` textual empty/status message. Approval actions are registered in the central Action Registry with `scope="explicit"`, no default chord, and the JavaScript keyboard dispatcher only executes `scope="app"` actions. The keymap table presents explicit actions as button-only and does not offer a shortcut editor for them.

This automated semantic contract is not NVDA verification. `NVDA_VERIFIED` remains false until a person tests the packaged exact candidate with NVDA on Windows.

## Security limits

HMAC authenticates evidence to this host process; it does not prove the real-world identity of the person operating the desktop. Authentication/login for a multi-user deployment is a separate future concern.

Authorization accounting is still in-memory and process-local. PR #61 provides thread-atomic budget/approval commit, not a durable distributed transaction. The external side effect happens after authorization and requires adapter-specific idempotency/recovery.

No claim is made that pywebview, WebView2 or HMAC is an operating-system sandbox. Untrusted execution remains subject to the separate M9/M10 isolation contracts.

## Acceptance gate

Before integration, the exact candidate must prove:

1. low-risk actions cannot be laundered into R4 requests;
2. pending UI views expose exact parameters but no issuer/signature/secret/evidence;
3. valid host-issued evidence authorizes the exact action;
4. forged signature, foreign issuer and different process secret fail closed;
5. denied and expired requests cannot issue evidence;
6. no trusted verifier means an approval-gated action is rejected;
7. Toolsmith high-impact actions can use only a host-injected verifier;
8. desktop approve/deny handlers accept request ID only and cannot override action parameters supplied by Python;
9. approval controls are semantic native buttons and have no global default shortcut;
10. complete Ubuntu + Windows repository verification and M12 packaged Windows semantic/UIA proof are green on the exact candidate.

`HUMAN_TESTED` and `NVDA_VERIFIED` remain false until real human acceptance of the exact packaged candidate.
