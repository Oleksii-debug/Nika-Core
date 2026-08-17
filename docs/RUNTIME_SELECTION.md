# M2 agent runtime selection — evidence record

Decision date: 2026-08-18.
Status: LangGraph selected as primary behind `AgentRuntimePort`; adapter/coordinator and real SQLite durability proof tests are prepared on the dependent M2 branch. Executable CI remains blocked by GitHub account Actions billing/spending infrastructure, so no M2 progress weight is credited yet.

## Decision
ADAPT LangGraph as Nika Core's primary durable orchestration runtime behind `AgentRuntimePort`.
KEEP Microsoft Agent Framework as the secondary adapter candidate and future migration/interop path; do not run both orchestration kernels in production simultaneously without measured need.

## Why LangGraph wins the current Windows-local target
1. LangGraph v1 keeps durable execution, checkpointing, persistence and human-in-the-loop as first-class behavior.
2. The official `langgraph-checkpoint-sqlite` package directly matches Nika's single-machine local SQLite recovery target.
3. LangGraph persistence stores step-level graph state and pending writes, which is the mechanism needed to resume without re-running already completed work.
4. Its low-level runtime lets Nika keep task IDs, permissions, audit, approvals and model routing as Nika-owned domain concepts.

## Microsoft Agent Framework comparison
Microsoft Agent Framework remains a serious production-grade alternative: stable Python core, workflows, typed routing, checkpointing, human-in-the-loop and multi-agent orchestration, plus Ollama support. It remains behind the same Nika runtime boundary so it can be introduced later without rewriting domain logic.

For the present Windows-local target LangGraph remains preferred because its official SQLite checkpoint package gives a smaller and more direct persistence surface for the first durable desktop proof.

## Nika-owned boundary
Framework types do not enter kernel/domain public APIs. Runtime code uses:
- `AgentRuntimePort` — async start/resume/cancel contract;
- `RuntimeRequest` — framework-neutral task/thread/payload/max-step input;
- `RuntimeResumeRequest` + `RuntimeResumeMode` — explicit continuation versus approval continuation;
- `RuntimeResult` and `RuntimeEvent` — normalized outcome/evidence;
- `RuntimeCapability` — advertise only behavior backed by evidence;
- `RuntimeRegistry` — selection without framework imports;
- `ReferenceRuntime` — deterministic no-LLM contract proof;
- `LangGraphRuntime` — thin compiled-graph adapter;
- `TaskRuntimeCoordinator` — maps normalized outcomes into Nika TaskQueue/AuditLog.

Cancellation is deliberately not advertised until a real cancellation behavior proof exists.

## Bounded execution
Nika `max_steps` maps to LangGraph's per-run `recursion_limit` for both initial execution and resume. Invalid non-positive bounds fail closed before framework execution.

## Framework-output isolation
Adapter output is recursively normalized to primitive/mapping/list values. Unknown framework objects become textual representations rather than leaking framework instances into Nika public result/audit contracts.

## Important defect found in this proof cycle — synchronous saver with async graph
The first M2 implementation paired `graph.ainvoke()` with LangGraph's synchronous `SqliteSaver`. Current official LangGraph source explicitly states that `SqliteSaver.aget_tuple`, `alist` and `aput` are not supported and raise `NotImplementedError`; asynchronous graph execution must use `AsyncSqliteSaver`.

This is now corrected before integration:
- `open_langgraph_sqlite()` is an async context manager;
- it opens `aiosqlite` and constructs `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`;
- `aiosqlite` is an explicit `agent` dependency;
- the helper owns and closes the async connection;
- `LANGGRAPH_STRICT_MSGPACK=true` is forced before checkpointer construction;
- checkpoint setup is awaited;
- CI for the M2 branch installs both development and agent extras so the real framework tests can run when GitHub runners become available.

This defect is evidence for the ports/adapters rule: the correction stays inside the LangGraph adapter/checkpoint boundary and does not require Nika domain changes.

## Real-framework durability proof now prepared
`tests/test_langgraph_real_durability.py` uses the actual LangGraph graph API and actual async SQLite checkpointer rather than fakes. It prepares three acceptance proofs:

1. **Completed-step persistence after runtime recreation**
   - a first node performs an externally visible side effect once;
   - a later node fails intentionally;
   - the SQLite connection, graph and runtime objects are destroyed;
   - fresh objects reopen the same checkpoint database;
   - `CONTINUE` resumes the same thread;
   - the failed node completes while the earlier side effect remains exactly once.

2. **Approval interrupt survives recreation**
   - a completed preparation node performs a side effect;
   - a second node calls LangGraph `interrupt()`;
   - the runtime/checkpointer objects are destroyed;
   - fresh objects reopen the same database;
   - Nika resumes with an approval value via `Command(resume=...)`;
   - the completed preparation side effect is not repeated.

3. **Corrupt checkpoint fails closed**
   - an approval-waiting thread is persisted;
   - its checkpoint blob is deliberately corrupted in SQLite;
   - a fresh runtime attempts resume;
   - Nika must return typed `FAILED` rather than silently starting a new thread;
   - the pre-interrupt side effect remains exactly once.

The existing deterministic adapter tests still cover thread ID, max-step bounds, result/interrupt normalization, ordinary and approval resume, exception normalization, output isolation and truthful capability advertisement. Coordinator tests cover TaskQueue and AuditLog mapping.

## Security
Strict checkpoint deserialization is a Nika requirement, not a user preference. Current LangGraph documentation warns that unrestricted checkpoint deserialization can permit code execution when checkpoint storage is compromised. Nika therefore forces strict MsgPack deserialization at its construction boundary.

## Remaining gate before M2 credit
When GitHub Actions runners are available:
1. execute Ruff and compile checks;
2. install `.[dev,agent]` and execute the full test suite;
3. fix any real-framework API/test failures found by execution;
4. run the real SQLite restart, interrupt/recreate/resume and corruption proofs;
5. run the adapter through real `TaskRuntimeCoordinator` persistence mapping;
6. define and prove cancellation before adding `RuntimeCapability.CANCELLATION`;
7. only after M1 is independently green/merged, rebase or retarget M2 onto green main and integrate through its own green PR.

Until executable evidence is green, M2 is IMPLEMENTED/PREPARED only, not INTEGRATED and receives no final A–Z percentage credit.

## Official sources checked 2026-08-18
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangGraph checkpoint reference: https://reference.langchain.com/python/langgraph/checkpoints
- LangGraph SQLite implementation: https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py
- LangGraph async SQLite implementation: https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py
- LangGraph SQLite README/security note: https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/README.md
- Microsoft Agent Framework overview: https://learn.microsoft.com/en-us/agent-framework/overview/
- Microsoft workflows: https://learn.microsoft.com/en-us/agent-framework/workflows/
- Microsoft Ollama provider: https://learn.microsoft.com/en-us/agent-framework/agents/providers/ollama
