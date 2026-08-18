# PROJECT STATUS — Nika Core

Updated: 2026-08-18
Canonical repository: Oleksii-debug/Nika-Core
Development mode: ACTIVE DEVELOPMENT

## Proven weighted progress
- M0 research/reuse/governance/bootstrap: GREEN / INTEGRATED, 100% of its 6% weight.
- Overall proven final A–Z product remains **6.0%**.
- M1 foundation candidate is IMPLEMENTED on `dev/m1-foundation` / PR #2 but not INTEGRATED; its 10% product weight is not credited until executable verification is green.
- M2 durable runtime remains IMPLEMENTED/PREPARED on dependent PR #3 but is not integrated and receives no percentage credit.

## Current milestone
M1 — kernel foundation integration gate.

## M1 candidate scope
- typed/versioned Pydantic Settings configuration;
- backward-compatible database path environment aliases and explicit configuration;
- ordered SQLite migrations and future-schema fail-closed behavior;
- persisted versioned Agent and Workspace registries;
- deterministic Audit Log;
- standard installed-workspace discovery contract;
- central Action Registry;
- persisted remappable Keymap with remap/unbind/restore/import/export/conflict detection;
- existing task/checkpoint behavior retained.

## Current source-review hardening batch
The GitHub Actions account-level runner blocker was not re-probed because the equivalent failure was checked recently and the canonical six-hour duplicate-probe policy is active.

Instead, M1 was hardened for the first executable runner/local verification:
- added `scripts/verify.py` as the single reproducible verification path for dependency consistency, Ruff, compileall and pytest;
- GitHub CI now invokes that same harness after installing milestone dependencies, reducing local/CI command drift;
- README documents the exact local verification command;
- source review found a real keymap defect: semantically identical shortcuts with reordered or aliased modifiers (for example `Ctrl+Shift+S` and `shift+control+s`) could evade duplicate/conflict detection;
- keymap canonicalization now normalizes modifier order/aliases and rejects ambiguous multi-primary-key or duplicate-modifier bindings;
- regression tests were added for equivalent modifier order/aliases and invalid shortcut shapes.

## Exact evidence
- Last green main baseline: `df48f70b738f9227cad1df08ce3d7f40115b5f08`.
- PR #2 source head before this status-only commit: `c10d88db63ee0c1e8238095847f2a3ec9168b119`.
- PR #2 remains open and mergeable into main.
- No new Ruff/compile/pytest result is claimed PASSED in this cycle because GitHub Actions did not execute and this automation environment did not obtain an authenticated local checkout.

## Current blocker
Most recent canonical GitHub Actions evidence remains the account billing/spending runner-allocation failure before checkout with no workflow steps executed. This is infrastructure evidence, not code-test evidence. Do not merge or credit M1 until the verification harness actually executes successfully on the exact candidate SHA.

## M2 dependency note
PR #3 targets `dev/m1-foundation`. Because M1 moved during this hardening batch, M2 must be synchronized/rebased onto the eventual green M1/main before its runtime suite is accepted. Do not duplicate the new M1 commits manually into production main.

## Packaging policy
No EXE in this cycle. Development remains Python/source-first. Windows standalone is built at milestone/user-test/release gates; final users must not need Python.

## Human-only gate
Real NVDA usability is never marked VERIFIED by automation.

## Next large coherent batch
1. Respect the duplicate infrastructure-probe interval.
2. When Actions can allocate a runner, execute PR #2 with `python scripts/verify.py`; fix any dependency/Ruff/compile/pytest failures in the same branch.
3. Merge M1 only after exact green evidence.
4. Synchronize PR #3 onto green main/M1 and execute the full real LangGraph/SQLite durability/recovery/cancellation/deadline/retry/idempotency suite.
5. Begin M3 only after M2 is executable-green and integrated.
