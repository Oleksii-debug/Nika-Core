# M10 downstream security, sandbox and reliability foundation

Status: IMPLEMENTED candidate on `dev-b/m10-security-sandbox`; no milestone credit before exact required CI evidence and integration.

## Scope

This slice adds defense-in-depth authorization immediately before downstream adapters without editing M1-M4 implementation.

Implemented:

- `SandboxPolicy` with a canonical workspace root, explicit writable sub-roots, exact network-host allowlist and executable-name allowlist;
- traversal-safe write resolution using normalized resolved paths;
- `ExecutionBudget` and `ExecutionBudgetLedger` for bounded write bytes, network calls and process launches;
- immutable `ActionIntent` with an exact SHA-256 approval fingerprint over tool, risk, target and side-effect parameters;
- expiring, timezone-aware `ApprovalEvidence` and one-time-use `ApprovalLedger`;
- high-impact actions always require matching explicit approval; individual lower-risk actions can additionally set `approval_required=True`;
- authorization order is fail-closed: tool grant -> path/network/process boundary -> approval -> resource reservation;
- deterministic regression tests for path escape, write-root violations, host/process allowlists, budgets, exact-action approval binding, expiry, replay and ungranted tools.

## Relationship to M4

M4 remains authoritative for standardized `ToolRisk` and its existing external/high-impact execution approval boundary. M10 does not weaken or replace it. The M10 guard is a downstream defense-in-depth layer intended for workspaces, plugins, computer-use adapters and coding workers before they reach the existing M4 executor.

This branch imports `ToolRisk` as a stable contract and does not edit `src/nika_core/tools.py` or other M1-M4 implementation.

## Security model

The policy deliberately separates four questions:

1. Is the tool explicitly granted to this downstream execution context?
2. Is the requested filesystem/network/process target inside its sandbox allowlist?
3. Does the action stay inside declared resource budgets?
4. If the operation is high-impact or specifically marked approval-required, does a fresh single-use approval match this exact action fingerprint?

A stale approval for a different target, amount, path, host, executable or action ID cannot authorize the new action because any such change modifies the fingerprint.

## REUSE / ADAPT / CUSTOM

- REUSE the integrated M4 `ToolRisk` vocabulary rather than creating a competing risk taxonomy.
- REUSE `pathlib.Path.resolve()` for canonical filesystem boundary checks.
- REUSE Python `hashlib.sha256` for deterministic approval-action binding.
- CUSTOM (thin): Nika-specific workspace allowlists, bounded downstream resource ledger and explicit approval evidence semantics.

This is policy/contract code only. It does not attempt to implement an operating-system security boundary by itself. Future coding/browser/Windows adapters must still run in the strongest practical process/container/worktree isolation supported by their adopted upstream implementation.

## Acceptance requirements

1. `python scripts/verify.py` passes on the exact candidate on Ubuntu and Windows.
2. Path traversal and writes outside declared roots fail closed.
3. Unknown hosts and executables fail closed.
4. Resource budgets cannot be exceeded silently.
5. High-impact approval is exact-action-bound, expiring and single-use.
6. Ungranted tools fail before resource reservation.
7. No secret/session/cookie/browser-profile material is introduced.
8. Exact green head is integrated before M10's 5% weight is credited.

PACKAGED, HUMAN_TESTED and NVDA_VERIFIED are not claimed by this source slice.
