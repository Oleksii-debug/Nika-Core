# M2 agent runtime selection — evidence record

Decision date: 2026-08-18.
Status: primary runtime selected; executable framework integration proof still blocked from independent GitHub CI by account Actions billing/spending infrastructure.

## Decision
ADAPT LangGraph as Nika Core's primary durable orchestration runtime behind `AgentRuntimePort`.
KEEP Microsoft Agent Framework as the secondary adapter candidate and future migration/interop path; do not run both orchestration kernels in production simultaneously without measured need.

## Why LangGraph wins the current Windows-local target
1. LangGraph v1 is explicitly stability-focused and retains durable execution, checkpointing, persistence and human-in-the-loop as first-class core behavior.
2. The official `langgraph-checkpoint-sqlite` package provides synchronous and asynchronous SQLite checkpoint savers. That directly matches Nika's single-machine Windows/SQLite recovery target with minimal extra infrastructure.
3. LangGraph persistence records graph state at execution steps and documents restart from the last successful step, including preservation of successful writes when sibling work fails.
4. Its low-level runtime makes it practical to keep Nika task IDs, permissions, audit, approvals and model gateway as Nika-owned domain concepts rather than framework-owned product state.

## Microsoft Agent Framework comparison
Microsoft Agent Framework is now a serious production-grade alternative: Python core is marked Production/Stable; workflows provide typed routing, checkpointing, human-in-the-loop and multi-agent orchestration; `FileCheckpointStorage` supports persistent checkpoint storage; and Ollama is officially documented.

For Nika's current target it scores lower because:
- the native Python Ollama package is currently published as prerelease, while local models are a first-class Nika requirement;
- the built-in persistence story documented for local workflows is file/checkpoint-storage oriented rather than a direct SQLite checkpoint package matching Nika's local store;
- adopting its broader workflow/provider surface now would add more framework surface before Nika's own domain contracts are stable.

This is not a rejection. The adapter boundary is intentionally preserved so Microsoft Agent Framework can be added later for specific workflows or replace the primary runtime if measured evidence becomes stronger.

## Nika-owned boundary
Framework types must not enter kernel/domain public APIs. New code uses:
- `AgentRuntimePort` — async run contract;
- `RuntimeRequest` — framework-neutral task/thread/payload/max-step/resume input;
- `RuntimeResult` and `RuntimeEvent` — normalized outcome/evidence;
- `RuntimeCapability` — feature-based selection;
- `RuntimeRegistry` — runtime registration/selection without framework imports.

A deterministic `ReferenceRuntime` exists to test the contract without an LLM or external runtime dependency.

## Selection evidence encoded in source
`runtime/selection.py` records the dated evidence matrix for LangGraph and Microsoft Agent Framework. It is not a permanent truth table: re-run this selection before a major runtime migration or if upstream stability/provider/persistence changes materially.

## Required executable proof before M2 receives progress credit
The primary adapter still must pass the same Nika scenario on an executable environment:
1. create a task/thread;
2. complete a deterministic side-effect-free step;
3. persist a checkpoint to local SQLite;
4. stop the process;
5. recreate runtime/process objects;
6. resume without repeating the completed step;
7. interrupt before a dangerous action;
8. persist WAITING_APPROVAL state;
9. approve/reject and resume deterministically;
10. cancel a run and confirm task/audit consistency.

Until that executable proof and CI are green, M2 is PREPARED/IMPLEMENTED only, not INTEGRATED and receives no final A–Z percentage credit.

## Official sources checked 2026-08-18
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph v1: https://docs.langchain.com/oss/python/releases/langgraph-v1
- LangGraph SQLite checkpoint package: https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite
- Microsoft Agent Framework overview: https://learn.microsoft.com/en-us/agent-framework/overview/
- Microsoft workflows: https://learn.microsoft.com/en-us/agent-framework/workflows/
- Microsoft WorkflowBuilder checkpointing: https://learn.microsoft.com/en-us/python/api/agent-framework-core/agent_framework.workflowbuilder
- Microsoft Ollama provider: https://learn.microsoft.com/en-us/agent-framework/agents/providers/ollama
