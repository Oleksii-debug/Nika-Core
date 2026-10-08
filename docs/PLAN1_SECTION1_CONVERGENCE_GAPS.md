# Plan 1 / Section 1 — adoption convergence evidence and remaining gates

Scope: **Nika Core / Plan 1 / Section 1 only**, legacy Section 1.
Base: `main@a82149694d5efa2fde1a4c3bb46c474b9c20c87e`.
Convergence parent: PR #1734 @ `ca8ab0b9d27a975fecf36a5452f268d9ce0ff820`.
Do **not** infer Plan 2–7 Section status from this file.

## Canonical reuse and source authorities

The adoption and historical reuse decisions remain in
`docs/THIRD_PARTY_ADOPTION.md`, `docs/REUSE_CATALOG_2026-08-18.md`
and `docs/PLAN1_SECTION1_ADOPTION_AUDIT.md` on PR #1734.
The machine-readable **declared dependency contract** is
`docs/PLAN1_DEPENDENCY_AUTHORITY.json`.

| Capability | Nika-owned authority | Existing source/adopted engine boundary |
|---|---|---|
| Runtime and orchestration | `AgentRuntimePort`, durable Nika session/effect state | `src/nika_core/runtime/contracts.py`, `langgraph_runtime.py`, `recovery.py`; LangGraph is an adapter, not domain truth |
| Scheduling | Nika job identity, policy, schedule store | `src/nika_core/scheduler/contracts.py`, `apscheduler_adapter.py`, `store.py` |
| Models | One `ModelGateway` with policy/routing | `src/nika_core/model_gateway/contracts.py`, `gateway.py`, `providers.py`, `foundry_local.py` |
| Planning | Deterministic Brain + approval-aware ToolExecutor | `src/nika_core/intelligence/contracts.py`; Unified Planning adapter is replaceable |
| Coding engines | Product Factory trusted project/review/effect gates | `src/nika_core/product_factory_coding_worker_adapter.py`; no standalone factory kernel |
| Browser / Windows UIA | Nika semantic interaction/permission boundary | `src/nika_core/interaction/playwright_adapter.py`, desktop bridge; browser/host event types stay at the edge |
| Media | Nika acquisition, provenance, delivery contracts | `src/nika_core/media/contracts.py`, `acquisition.py`, `transcription.py`; optional yt-dlp and subtitle workers |
| Documents | Nika source/provenance and workspace result DTOs | pypdf, python-docx, openpyxl, defusedxml in declared base requirements |
| Windows packaging | Nika artifact identity, release/attestation/rollback policy | `src/nika_core/packaging/windows.py`, `release.py`, `attestation.py`; PyInstaller is tooling |
| Web/Cloud | Nika application-service, task, identity and policy contracts | Transport adapters may be adopted in Plan 6, never replace Plan 1 core authority |

## Automated drift guard

`tests/test_plan1_dependency_authority.py` compares each base/extra
requirement string to the manifest and rejects undeclared dependency changes,
missing/duplicated groups, invalid authority decisions, unsupported package
maintenance/license claims, altered Python compatibility and false lock status.
The build-system's `setuptools.build_meta` backend and the declared
`setuptools>=75` / `wheel` build requirements are now inventoried separately
from runtime dependencies. The guard rejects forged build-only activation,
backend substitution, omitted build requirements, and pyproject build-input
drift. This is a constraint-boundary check, **not** a reproducible wheel build,
resolved build-environment lock, install proof, or dependency license clearance.
It uses Python standard-library `tomllib` and `json` plus repository pytest.

`tests/test_plan1_architecture.py` on the parent PR guards direct vendor
imports into six Nika-owned port/contract modules, with negative tests for
nested/dynamic imports. Those tests are not a substitute for integration or
runtime trust-boundary tests.

## Added Section 1 drift/adversarial admission evidence (2026-10-08)

- The dependency-adoption guard now pins the reviewed `REUSE`/`ADAPT`
  decision and canonical Nika owner for all 14 current groups, plus the
  allowlisted decisions rejecting five competing runtime/domain authorities.
  A nonempty manifest label is no longer sufficient to silently rewrite an
  approved architecture decision.
- New negative cases cover forged framework decisions, vendor-owned policy
  labels, optional-provider reassignment and removed rejection entries.
- The inherited AST architecture guard now recognizes direct or aliased
  `builtins.__import__` and `from builtins import __import__ as ...`
  import paths, including nonliteral arguments, in the designated stable
  Nika contract modules. This is a targeted guard, not an all-path Python
  runtime sandbox.
- Exact GitHub commits `50e494daa0c862c97191f47747e04c86d6ac8213`
  and `73aed268fa2c6744e4b36c8bd453021d47a72111` were
  persisted on the existing Section-1 convergence branch; source blob
  readback was completed. Authored negative tests are not a CI PASS.
- These changes neither add new dependencies nor a second orchestration,
  model, scheduler, policy or persistence authority.

## Deliberate non-claims — currently NOT terminal DONE

1. `pyproject.toml` declares ranges; this checkout has **no committed
   resolution lock** for complete transitive dependency graphs on supported
   Python/Windows/Linux profiles. The manifest is **not** an installable
   deterministic lock. Exact resolved versions/hashes are not verified.
2. Package-by-package upstream maintenance, adopted exact distribution license
   and redistributable notices require independent version-specific proof.
   Candidate/reference-only engines must not be counted as installed.
3. The PR's exact-head Core CI Ubuntu/Windows, integrity and integration
   readback must be green and reconciled before terminal closure. A queued
   check, source readback or green ancestor is not a pass.
4. Real packaged Windows/NVDA and real Web/Cloud/Node final acceptance belongs
   to Plan 7; no such evidence is asserted here.

Status: **ACTIONABLE / PARTIAL**, preserving terminal truth. Do not update
`MULTI_PLAN_CLOSURE_STATE.md` or the assigned Drive plan to DONE until the
remaining applicable Section 1 evidence passes and the exact integration
commit has been read back. Section 2 remains next in order, not skipped.
