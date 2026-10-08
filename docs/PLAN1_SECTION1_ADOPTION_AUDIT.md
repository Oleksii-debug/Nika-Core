# Plan 1 — Section 1: adoption and architecture audit (2026-10-08)

Scope: **only Plan 1, Section 1** (former architecture/reuse section).  The
binding architecture is `docs/MASTER_SPEC.md`, `docs/FULL_PRODUCT_VISION_2026-08-19.md`,
`docs/WEB_CLOUD_PRODUCT_ARCHITECTURE.md` and
`docs/REUSE_CATALOG_2026-08-18.md`.  This document records the current
source-level implementation trace rather than treating the old catalog as
proof of integrated behavior.

## REUSE → REPAIR → CONVERGE audit

| Nika-owned invariant | Reused/adapted component | On-main source / applicable automated evidence |
|---|---|---|
| Typed config and validated environment | Pydantic Settings; `platformdirs` | `src/nika_core/config.py`; `tests/test_m1_foundation.py` |
| Local deterministic persistent state; ordered migrations | Python `sqlite3` | `src/nika_core/data/sqlite.py`, `src/nika_core/data/schema.py`; `tests/test_m1_foundation.py` |
| Durable agent runtime behind replaceable port | LangGraph adapter, reference runtime | `src/nika_core/runtime/contracts.py`, `langgraph_runtime.py`, `reference.py`; `tests/test_runtime_contracts.py`, `tests/test_langgraph_adapter.py` |
| Policy, approval and audit authority outside models | Nika-specific thin contracts/policy | `src/nika_core/security/policy.py`, `src/nika_core/kernel/audit.py`; `tests/test_security_policy.py` |
| Replaceable model/provider contracts | HTTPX and optional Foundry/Ollama adapters | `src/nika_core/model_gateway/contracts.py`, `gateway.py`; `tests/test_model_gateway_api_route.py` |
| Installed workspace discovery without eager import | `importlib.metadata` entry points | `src/nika_core/kernel/workspace_plugin.py`; `tests/test_m1_foundation.py` |
| Stable plugin/tool declarations behind approval | Pydantic and optional MCP SDK | `src/nika_core/plugins/sdk.py`, `src/nika_core/mcp_boundary.py`; `tests/test_plugin_workspace_sdk.py`, `tests/test_m4_mcp_safety.py` |
| User-remappable semantic application actions | Nika-owned Action Registry/Keymap | `src/nika_core/kernel/action_registry.py`; `tests/test_m1_foundation.py` |
| Platform-neutral ProductProject presentation | Pydantic Nika-owned DTOs, adapters | `src/nika_core/product_command/contracts.py`; `tests/test_product_command_contracts.py` |

The architecture keeps generic implementation engines in adapters and Nika's
task/identity/safety/recovery policy in core services.  It **does not** introduce
another persistence, scheduler, ModelGateway or AgentRuntime authority.

## Executable regression evidence

`tests/test_plan1_architecture.py` tests that six designated stable contract
modules do not acquire *direct* imports of orchestration/model/HTTP/MCP/Windows/Web
engines. It includes negative tests for aliased and nested imports, false-positive
documentation and a malformed-source fail-closed case. This is a structural guard;
it cannot prove absence of dynamic imports or prove packaged/NVDA acceptance.

Existing `tests/test_m1_foundation.py` already checks schema migration from v1,
future-schema rejection, agent/workspace persistence, audit round-trip and shortcut
conflicts.  `tests/test_runtime_contracts.py` tests runtime port behavior and
rejects invalid resume requests.  CI workflow `.github/workflows/ci.yml` runs
`scripts/verify.py` under Ubuntu and Windows.

## Exact pre-change authority

Audited default-branch SHA:
`a82149694d5efa2fde1a4c3bb46c474b9c20c87e`.

Core CI main run `37719754719` for that SHA is reported successful.
This **does not** certify later candidate commits. Each later PR/merge requires
its own exact-head evidence; no source-level guard earns human Windows/NVDA or
web/cloud product-evidence credit.

Status: acceptance of this Section must be decided from the exact current
integration SHA and passed fresh checks, and recorded in
`MULTI_PLAN_CLOSURE_STATE.md` only then.
