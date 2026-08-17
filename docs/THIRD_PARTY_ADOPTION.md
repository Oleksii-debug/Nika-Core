# Third-party adoption policy — reuse before rewrite

Canonical rule: before implementing a new subsystem, search current official documentation and maintained upstream projects for a reusable component. Adopt or adapt a maintained library when it satisfies our contract and license/security requirements. Write custom code only for Nika-specific policy, glue, accessibility, safety, or a missing capability.

Do not vendor random copied source into the repository. Prefer package dependencies with version constraints and a lock file, preserving upstream update/security paths and license metadata.

## Adopt
- LangGraph: primary durable orchestration runtime; persistence, loops, branching, parallel execution and human-in-the-loop.
- langgraph-checkpoint-sqlite: local graph checkpoints; enable strict deserialization policy.
- Deep Agents: selective planning/subagent/filesystem/memory/permission harness behind Nika interfaces.
- LiteLLM: provider normalization inside ModelGateway, including Ollama/cloud adapters, normalized errors and routing/fallback where needed.
- MCP Python SDK v2: standard tool/resource interoperability; still governed by Nika permissions.
- APScheduler stable 3.x: implementation behind SchedulerPort.
- PySide6/Qt Widgets: accessible Windows GUI baseline.
- pyside6-deploy/Nuitka: standalone Windows distribution at packaging gates.
- DSPy: optional M8 optimizer only when explicit metrics/evaluation datasets exist.

## Evaluated but not primary kernel runtimes
- Microsoft AutoGen: strong teams/memory/event-driven agent framework, retained as future adapter/research option.
- CrewAI: strong crews/flows/memory/guardrails, retained as future adapter/research option.
Running multiple orchestration runtimes inside the kernel would duplicate state/checkpoint/team semantics and increase integration debt.

## Write custom code only for
Nika domain schemas/IDs; public workspace/plugin contract; audit/permission/approval policy; accessible Windows UX; artifact/release metadata; target-PC resource policy; experiment promotion governance; thin adapters where upstream APIs do not match stable Nika interfaces.

## Mandatory pre-code record
Every new subsystem decision must be classified as REUSE, ADAPT or CUSTOM before implementation. CUSTOM requires a short explanation of why maintained upstream options do not satisfy the requirement.
