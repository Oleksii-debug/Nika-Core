# M2 agent runtime selection — evidence record

Decision date: 2026-08-18.
Status: LangGraph selected as primary behind `AgentRuntimePort`; adapter/coordinator, async SQLite durability proofs and bounded in-flight cancellation proofs are prepared on the dependent M2 branch. Executable CI remains blocked by GitHub account Actions billing/spending infrastructure, so no M2 progress weight is credited yet.

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

## Bounded execution
Nika `max_steps` maps to LangGraph's per-run `recursion_limit` for both initial execution and resume. Invalid non-positive bounds fail closed before framework execution.

## Framework-output isolation
Adapter output is recursively normalized to primitive/mapping/list values. Unknown framework objects become textual representations rather than leaking framework instances into Nika public result/audit contracts.

## Important defect found in this proof cycle — synchronous saver with async graph
The first M2 implementation paired `graph.ainvoke()` with LangGraph's synchronous `SqliteSaver`. Current official LangGraph source explicitly states that the synchronous saver does not support its asynchronous checkpoint API; asynchronous graph execution must use `AsyncSqliteSaver`.

This is corrected before integration:
- `open_langgraph_sqlite()` is an async context manager;
- it opens `aiosqlite` and constructs `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`;
- `aiosqlite` is an explicit `agent` dependency;
- the helper owns and closes the async connection;
- `LANGGRAPH_STRICT_MSGPACK=true` is forced before checkpointer construction;
- checkpoint setup is awaited;
- CI for the M2 branch installs both development and agent extras so the real framework tests can run when GitHub runners become available.

This defect is evidence for the ports/adapters rule: the correction stays inside the LangGraph adapter/checkpoint boundary and does not require Nika domain changes.

## Real-framework durability proofs prepared
`tests/test_langgraph_real_durability.py` uses the actual LangGraph graph API and actual async SQLite checkpointer rather than fakes. It prepares four acceptance scenarios:

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
   - runtime/checkpointer objects are destroyed;
   - fresh objects reopen the same database;
   - Nika resumes with an approval value via `Command(resume=...)`;
   - the completed preparation side effect is not repeated.

3. **Corrupt checkpoint fails closed**
   - an approval-waiting thread is persisted;
   - its checkpoint blob is deliberately corrupted in SQLite;
   - a fresh runtime attempts resume;
   - Nika must return typed `FAILED` rather than silently starting a new thread;
   - the pre-interrupt side effect remains exactly once.

4. **Active run cancellation**
   - a real LangGraph async node waits without completing;
   - Nika tracks the exact in-flight `asyncio.Task` by `(task_id, thread_id)`;
   - `cancel()` cancels that exact invocation and waits for cancellation propagation;
   - the public result becomes typed `CANCELLED`;
   - the active registry is cleaned so a second cancel returns `False`;
   - duplicate concurrent execution of the same task/thread is rejected before a second graph invocation begins.

## Cancellation scope and truthfulness
`RuntimeCapability.CANCELLATION` now means **bounded in-process cancellation of the currently active invocation**. It does not claim that an arbitrary half-finished node can later be resumed from the exact instruction where cancellation happened.

Current LangGraph timeout/cancellation behavior is based on asyncio cooperative cancellation; blocking synchronous/CPU work may delay delivery until control returns to the event loop. Upstream discussion also documents a persistence risk if a caller cancels and then tears down the process before pending checkpoint/background writes have drained. Therefore Nika does not advertise "resume-after-mid-step-cancel" as a durability guarantee. Safe restart/resume remains tied to persisted checkpoints at proven boundaries.

`TaskRuntimeCoordinator.cancel()` records `runtime.cancel_requested`; successful active cancellation is finalized by the already-running coordinator path as `RuntimeOutcome.CANCELLED` -> `TaskState.CANCELLED` plus normal `runtime.finished` audit evidence. A cancel request for a non-active task/thread is recorded as `runtime.cancel_not_active` and does not fabricate a state transition.

## Security
Strict checkpoint deserialization is a Nika requirement, not a user preference. Current LangGraph checkpoint guidance warns about unsafe deserialization when checkpoint storage is compromised. Nika therefore forces strict MsgPack deserialization at its construction boundary.

## Remaining gate before M2 credit
When GitHub Actions runners are available:
1. execute Ruff and compile checks;
2. install `.[dev,agent]` and execute the full test suite;
3. fix any real-framework API/test failures found by execution;
4. run the real SQLite restart, interrupt/recreate/resume, corruption and real cancellation proofs;
5. run the adapter through real `TaskRuntimeCoordinator` persistence mapping;
6. only after M1 is independently green/merged, rebase or retarget M2 onto green main and integrate through its own green PR.

Until executable evidence is green, M2 is IMPLEMENTED/PREPARED only, not INTEGRATED and receives no final A–Z percentage credit.

## Official sources checked 2026-08-18
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangGraph checkpoint reference: https://reference.langchain.com/python/langgraph/checkpoints
- LangGraph SQLite implementation: https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py
- LangGraph async SQLite implementation: https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py
- LangGraph timeout/cancellation implementation note: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py
- LangGraph cancellation persistence discussion: https://github.com/langchain-ai/langgraph/issues/5672
- LangGraph SQLite README/security note: https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/README.md
- Microsoft Agent Framework overview: https://learn.microsoft.com/en-us/agent-framework/overview/
- Microsoft workflows: https://learn.microsoft.com/en-us/agent-framework/workflows/
- Microsoft Ollama provider: https://learn.microsoft.com/en-us/agent-framework/agents/providers/ollama
