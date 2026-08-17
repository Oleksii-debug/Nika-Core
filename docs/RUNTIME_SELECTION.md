# M2 agent runtime selection — evidence record

Decision date: 2026-08-18.
Status: LangGraph selected as primary; Nika adapter/integration boundary implemented on dependent dev branch; executable real-framework durability proof still blocked from independent GitHub CI by account Actions billing/spending infrastructure.

## Decision
ADAPT LangGraph as Nika Core's primary durable orchestration runtime behind `AgentRuntimePort`.
KEEP Microsoft Agent Framework as the secondary adapter candidate and future migration/interop path; do not run both orchestration kernels in production simultaneously without measured need.

## Why LangGraph wins the current Windows-local target
1. LangGraph v1 is stability-focused and retains durable execution, checkpointing, persistence and human-in-the-loop as first-class core behavior.
2. The official `langgraph-checkpoint-sqlite` package provides local SQLite checkpoint savers. That directly matches Nika's single-machine Windows/SQLite recovery target with minimal extra infrastructure.
3. LangGraph persistence records graph state at execution steps and supports restart/resume from persisted checkpoints.
4. Its low-level runtime makes it practical to keep Nika task IDs, permissions, audit, approvals and model gateway as Nika-owned domain concepts rather than framework-owned product state.

## Microsoft Agent Framework comparison
Microsoft Agent Framework is a serious production-grade alternative: Python core is marked Production/Stable; workflows provide typed routing, checkpointing, human-in-the-loop and multi-agent orchestration; persistent checkpoint storage is documented; and Ollama is officially documented.

For Nika's current target it scores lower because:
- the native Python Ollama package is currently published as prerelease, while local models are a first-class Nika requirement;
- the built-in local persistence story is less directly aligned with Nika's SQLite-first desktop target;
- adopting its broader workflow/provider surface now would add more framework surface before Nika's own domain contracts are stable.

This is not a rejection. The adapter boundary is intentionally preserved so Microsoft Agent Framework can be added later for specific workflows or replace the primary runtime if measured evidence becomes stronger.

## Nika-owned boundary now implemented
Framework types do not enter kernel/domain public APIs. Runtime code uses:
- `AgentRuntimePort` — async start/resume/cancel contract;
- `RuntimeRequest` — framework-neutral task/thread/payload/max-step input;
- `RuntimeResumeRequest` + `RuntimeResumeMode` — explicit normal continuation versus human-approval continuation, also bounded by max steps;
- `RuntimeResult` and `RuntimeEvent` — normalized outcome/evidence;
- `RuntimeCapability` — feature-based selection; a runtime may advertise only behavior backed by evidence;
- `RuntimeRegistry` — runtime registration/selection without framework imports;
- `ReferenceRuntime` — deterministic no-LLM contract proof;
- `LangGraphRuntime` — thin adapter around a compiled LangGraph graph, normalizing completion/failure/interrupt results without exposing LangGraph types to Nika callers;
- `TaskRuntimeCoordinator` — maps normalized runtime outcomes/events into Nika TaskQueue state transitions and AuditLog entries.

`LangGraphRuntime` deliberately does not advertise cancellation yet. Returning `False` for cancel is safer than claiming behavior not proven by a real adapter test.

## Bounded execution
Nika `max_steps` is mapped to LangGraph's per-run `recursion_limit` for both initial execution and resume. A resume request cannot silently lose the task bound. Invalid non-positive bounds fail closed at the Nika contract before framework execution.

## Framework-output isolation
Adapter output is recursively normalized to primitive/mapping/list values. Unknown framework objects are converted to textual representations rather than leaking arbitrary framework instances into Nika public result/audit contracts. This keeps storage, UI and future runtime adapters independent of LangGraph object types.

## Local SQLite checkpointer security
`open_langgraph_sqlite()` is the Nika-owned construction boundary for the first LangGraph SQLite checkpointer. It:
- creates parent directories;
- opens SQLite with `check_same_thread=False`, matching upstream guidance for `SqliteSaver`;
- forces `LANGGRAPH_STRICT_MSGPACK=true` before saver construction; an insecure pre-existing false value is overridden because strict deserialization is a Nika security requirement;
- initializes the saver and closes the connection on construction failure;
- returns an explicit closeable/context-managed handle so the database connection lifecycle is not leaked.

Strict checkpoint deserialization is a security requirement, not an optional UI setting.

## Adapter/integration deterministic tests prepared
Tests cover:
- correct thread-id and per-run step-bound configuration;
- completed-output normalization;
- interrupt -> `WAITING_APPROVAL` normalization and stable resume token;
- approval resume through an injected command factory with a retained step bound;
- ordinary continuation with `None` input;
- runtime exceptions -> typed `FAILED` result;
- no unsupported cancellation capability claim;
- opaque framework objects cannot leak through normalized output;
- SQLite helper forces strict deserialization before saver construction and owns the connection lifecycle;
- coordinator maps RUNNING -> WAITING_APPROVAL -> RUNNING -> COMPLETED into Nika task state;
- coordinator records runtime lifecycle/approval evidence in AuditLog;
- unexpected runtime exceptions fail the Nika task closed to FAILED;
- non-approval resume is rejected before changing a waiting-approval task.

These tests are source-prepared but cannot be credited as green until an executable test runner actually runs them.

## Required real-framework proof before M2 receives progress credit
1. install the pinned M2 LangGraph/checkpoint dependencies;
2. create a deterministic graph and local SQLite checkpoint database;
3. complete one task step and persist it;
4. terminate/recreate graph/checkpointer/process objects;
5. resume the same thread without repeating the completed step;
6. interrupt before a simulated dangerous action;
7. persist the waiting state;
8. recreate runtime objects again;
9. approve and resume via `Command(resume=...)` using the same thread ID;
10. verify no completed side effect was repeated;
11. test corrupt/invalid checkpoint fail-closed behavior;
12. run the real adapter through `TaskRuntimeCoordinator` and verify TaskQueue/AuditLog mapping;
13. prove cancellation semantics before adding the `CANCELLATION` capability.

Until that executable proof and CI are green, M2 is IMPLEMENTED/PREPARED only, not INTEGRATED and receives no final A–Z percentage credit.

## Official sources checked 2026-08-18
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangGraph v1: https://docs.langchain.com/oss/python/releases/langgraph-v1
- LangGraph SQLite source/docs: https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite
- Microsoft Agent Framework overview: https://learn.microsoft.com/en-us/agent-framework/overview/
- Microsoft workflows: https://learn.microsoft.com/en-us/agent-framework/workflows/
- Microsoft WorkflowBuilder checkpointing: https://learn.microsoft.com/en-us/python/api/agent-framework-core/agent_framework.workflowbuilder
- Microsoft Ollama provider: https://learn.microsoft.com/en-us/agent-framework/agents/providers/ollama
