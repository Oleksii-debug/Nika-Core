# M10 downstream security, trusted approval and review authority

Status: CURRENT CANDIDATE on `work/oneshot10/m10-r4-trusted-authority`; independent AUD02 clearance and exact green CI are required before integration or milestone credit.

## Scope

M10 is the canonical host-owned approval/security boundary for dangerous downstream execution and exact trusted human review. It extends the integrated M4 `ToolRisk` vocabulary rather than creating a second approval framework.

The current candidate provides:

- `SandboxPolicy` with a canonical workspace root, explicit writable sub-roots, exact network-host allowlist and executable allowlist;
- `ExecutionBudget` / `ExecutionBudgetLedger` for bounded write bytes, network calls and process launches;
- immutable `ActionIntent` with an unambiguous versioned JSON SHA-256 fingerprint over every execution-relevant field, including `approval_required` and explicit `null` values;
- host-issued, HMAC-authenticated `ApprovalEvidence` with exact `approval_id`, `request_id`, `issuer_id`, fingerprint, approval time and expiry;
- one-shot action approval with replay state owned by the trusted host, not by caller-provided evidence or a caller-created ledger;
- atomic in-process/thread authorization of trusted approval consumption and resource-budget consumption under a fixed lock order;
- immutable `ReviewSubject` with exact project, purpose, resource and sorted consumer-specific bindings;
- host-issued exact review evidence plus framework-neutral verifier ports for Product Factory/Business Factory consumers;
- a `ProjectPurposeReviewVerifier` adapter structurally compatible with PF10-style `(project_id, evidence_ref, purpose)` review ports;
- an `ExactReviewVerifier` for PF6/Toolsmith consumers that need full release/environment/work/resource identity bindings;
- a keyboard-operable pending-approval path in the existing packaged HTML/pywebview shell using standard semantic buttons, without changing DEV04 UIA ownership;
- a corrected M4 `ToolExecutor` boundary where caller-supplied `ToolCall.approved=True` is compatibility metadata only and can never authorize an external/high-impact effect.

## REUSE -> ADAPT -> CUSTOM (thin)

### REUSE

- integrated M4 `ToolRisk` and `ToolExecutor` host approval-policy injection;
- historical PR #61's correct atomic validation/commit model and exact-action framing;
- historical PR #62's host-owned HMAC issuer, pending request lifecycle, bounded TTL and safe UI request projection;
- Python standard-library `hashlib`, `hmac`, `secrets`, `json`, `datetime` and `threading.RLock`;
- existing Nika `SecurityPolicy`, Toolsmith bridge, Action Registry, pywebview bridge and packaged HTML shell.

### ADAPT

Historical #61/#62 were old/stacked and were not merged as-is. Their valid invariants are adapted to current main:

- the old delimiter fingerprint is replaced by a versioned JSON encoding so tuple-boundary collisions cannot occur;
- `approval_required` is part of the fingerprint;
- approval replay state is moved into the trusted host verifier so creating a fresh caller-side `ApprovalLedger` cannot replay already-consumed signed evidence;
- host replay state, local defense-in-depth replay state and execution budget are committed in one critical section only after all validation succeeds;
- the HMAC key is process-instance ephemeral, even when a deterministic seed is supplied for tests, so restart always invalidates old evidence;
- review authority is generalized as `ReviewSubject` rather than hard-coded to PF6 or PF10 framework/domain types;
- the existing ToolExecutor approval callback remains the M4 execution hook, but caller `approved=True` no longer bypasses it.

### CUSTOM (thin)

Only Nika-specific policy is custom: exact action/review identity, bounded authority lifecycle, replay semantics, consumer adapters, and safe UI projection. No generic IAM, cryptographic vault, workflow engine or approval framework is reimplemented.

## Exact identities

### ActionIntent

The action fingerprint is SHA-256 over a version-tagged JSON sequence containing, in order:

1. action ID;
2. tool ID;
3. risk class;
4. exact target;
5. write path or `null`;
6. write byte count;
7. network host or `null`;
8. executable or `null`;
9. `approval_required`.

Changing any field produces a different fingerprint. Separator characters inside field values cannot change tuple boundaries.

### ReviewSubject

A review subject binds:

- `subject_kind`;
- `project_id`;
- `purpose`;
- `resource_id`;
- sorted unique `(binding_key, binding_value)` pairs.

PF6 should bind full release/environment/artifact/provider-relevant immutable identity in `bindings`; it must not reduce production promotion authority to a SHA-only or caller-provided reference. PF10's current project/purpose review port can use the canonical project-purpose adapter. Other consumers should use the exact subject verifier when their authority identity is richer.

## Host authenticity and replay

Positive action authority can originate only from an `ApprovalAuthority` composed by the trusted Python host. Runtime/model/tool callers may construct data objects, including legacy `ToolCall.approved=True`, but those values are not authority.

Action approval validation requires:

- expected host issuer;
- exact current action fingerprint;
- valid host HMAC;
- current time inside the approval interval;
- approval not previously consumed by the host.

On success, the host replay marker, local defense-in-depth replay marker and budget reservation commit atomically inside one process. If any validation or budget check fails, none of those three are consumed.

Review evidence is an exact attestation, not a one-shot execution token. It may be verified more than once during its bounded lifetime, but only for the identical `ReviewSubject`. Replacement project/purpose/resource/bindings, unknown evidence references, expired evidence and evidence from a restarted authority fail closed.

## Atomicity boundary

The atomic guarantee is intentionally narrow and explicit:

- covered: concurrent threads within one trusted Nika process, approval replay state, local replay state and `ExecutionBudgetLedger` reservation;
- not covered: distributed processes, provider effects, filesystem/database effects outside this critical section, or crash-atomic external execution.

Consumers that perform external effects still require their own durable pre-effect intent/effect journal and inspect/reconcile semantics. M10 must not be cited as proof of external-effect atomicity.

## Lifetime, issuer and restart semantics

- default request lifetime: 5 minutes;
- maximum request lifetime: 15 minutes;
- all timestamps are timezone-aware;
- every request/evidence item has independent opaque identity;
- the effective HMAC key includes fresh process-instance randomness and is never serialized;
- pending requests, issued evidence and host replay state are process-local;
- application/process restart invalidates all old action and review evidence, even if an issuer label or test seed is reused;
- after restart, a new human decision is required.

This fail-closed restart rule is deliberate. Persisting positive authority would require a separately protected durable authority store and recovery protocol; M10 does not silently invent one.

## R0-R4 and least privilege

M10 does not create a competing risk taxonomy. The existing `ToolRisk` contract and higher-level Nika R0-R4 policy remain authoritative.

Operational boundary in this candidate:

- read-only/local bounded work remains subject to explicit grants, sandbox and budget; any individual action can still be marked `approval_required=True`;
- external side effects and high-impact ToolExecutor calls cannot be authorized by caller data and require a positive trusted-host approval policy;
- `HIGH_IMPACT` actions at the downstream M10 guard always require exact signed one-shot evidence;
- standing permissions may narrow repeated low-risk prompts but cannot mint trusted evidence, widen their own scope, or bypass mandatory high-impact review;
- no approval path widens tool grants, sandbox roots, network hosts, executable allowlists or budget ceilings.

Consumers must continue to map their own exact R0-R4 semantics to these gates. A caller-provided string such as `approval_ref`, `approved=true`, evidence ID, project ID or purpose is never sufficient positive authority by itself.

## Framework-neutral consumer ports

`ApprovalVerifier` is the action-execution verifier used by `SecurityPolicy` and injectable through the Toolsmith security bridge.

`ExactReviewVerifier` verifies a complete immutable `ReviewSubject` plus opaque evidence reference and is the preferred PF6-style boundary.

`ProjectPurposeReviewVerifier` exposes only:

```text
verify(project_id, evidence_ref, purpose) -> bool
```

and is intended for existing PF10-style structural ports. It reconstructs the canonical subject inside the trusted adapter; callers do not submit signatures, issuer IDs or positive booleans.

## UI and secret boundary

The existing Windows HTML/pywebview shell exposes pending action/review requests as sanitized read-only views. The view contains only fields the human needs to evaluate the exact request plus an opaque `request_id`.

The browser/JS side never receives:

- HMAC key/seed;
- signature;
- approval evidence object;
- issuer secret/protected-store handle;
- a caller-controlled `approved` authority bit.

Approve/deny controls are ordinary semantic `<button>` elements reachable through normal keyboard navigation. Approval actions use Action Registry `scope="explicit"` with no default global binding, so a global shortcut cannot silently approve a dangerous request. This source-level semantic path is not HUMAN_TESTED or NVDA_VERIFIED; those labels require real human/NVDA evidence.

## Security boundaries and non-claims

HMAC proves that evidence came from the current trusted host authority instance. It does not prove a human's cryptographic identity and does not protect against arbitrary malicious code that has already achieved unrestricted execution inside the trusted Python host process.

M10 does not:

- expose or persist raw secrets;
- persist positive approval/review authority across restart;
- make external effects crash-atomic;
- replace provider-specific reconciliation;
- grant deployment, financial, credential or account authority by itself;
- bypass PF6/PF10/Toolsmith consumer-specific identity and policy checks;
- claim DEV04 UIA coverage or real NVDA verification.

## Acceptance/security replay

The candidate must prove at exact head:

1. caller-constructed signed-looking evidence fails;
2. `ToolCall.approved=True` cannot authorize external/high-impact execution;
3. wrong issuer, wrong exact action, expired and reused action approvals fail;
4. a fresh caller-side approval ledger cannot replay host-used evidence;
5. failed budget reservation does not burn valid approval;
6. concurrent approvals competing for a bounded budget produce only permitted commits;
7. review evidence rejects wrong project, purpose, resource, binding and replacement identity;
8. denied/expired/restarted review authority fails closed;
9. PF10 structural adapter rejects caller-fabricated evidence refs;
10. pending UI projection contains no secret/signature/evidence authority;
11. approval controls are explicit semantic controls with no global approval hotkey;
12. dependency consistency, lint/format, compile, focused tests, full Core CI and applicable M11/M12 gates are green on the exact candidate/current-main combination;
13. independent AUD02 review clears the candidate before merge.

PACKAGED product completeness, HUMAN_TESTED and NVDA_VERIFIED are not claimed by this source slice.
