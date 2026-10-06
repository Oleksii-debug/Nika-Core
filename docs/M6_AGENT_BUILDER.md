# M6 Agent Builder and permissions

Status: implementation candidate on `dev/m6-agent-builder-permissions` until exact CI evidence is green and integrated.

## Reuse decision
- **REUSE** Pydantic v2 models, validators and JSON Schema export for the versioned declarative agent definition.
- **ADAPT** the integrated Nika `ModelGateway` for natural-language drafting. The model returns only a proposed JSON document; it never decides permission truth or activation.
- **REUSE** the existing Nika `ToolSpec` / `ToolRisk` registry as the authoritative tool classification input.
- **REUSE** SQLite migrations and `AuditLog` for durable version/activation evidence.
- **CUSTOM (thin)** deterministic R0–R4 compilation, immutable draft versioning and activation approval policy because these are Nika-specific product/safety semantics.

No new third-party dependency is needed for this slice.

## Contract
`AgentDefinition` format version 1 contains stable agent identity/version, name, goal, instructions, model profile, optional schedule and resource-budget references, bounded max steps, and declarative tool grants. Unknown fields are rejected and duplicate tool grants are invalid.

The draft path is deliberately separated from the compiler:

1. natural-language request -> `AgentDraftService` -> `ModelGateway`;
2. returned JSON -> strict `AgentDefinition` validation;
3. `AgentCompiler` resolves model/schedule/resource references and every tool against current registries;
4. the registered `ToolRisk` is mapped to Nika R0–R4 and must match the declared grant exactly;
5. R4 tools become explicit human-approval requirements;
6. the compiled draft, including approval requirements and highest risk, is persisted immutably;
7. activation reads those persisted requirements and fails closed until all R4 approvals are explicitly supplied.

The LLM cannot lower a registered tool's risk, invent a tool, invent a model/schedule/resource reference, or silently authorize a dangerous capability.

## R0–R4 mapping for current standardized tools
- R0: `ToolRisk.READ_ONLY`.
- R1: `ToolRisk.LOCAL_WRITE`.
- R2: `ToolRisk.EXTERNAL_SIDE_EFFECT`.
- R3: reserved for a future explicit sensitive-data capability class; no current standardized `ToolRisk` maps here.
- R4: `ToolRisk.HIGH_IMPACT`; never activates without explicit human approval and still remains subject to execution-time approval in the tool layer.

This preserves the existing M4 execution boundary rather than replacing it. Agent Builder approval allows a versioned agent configuration to expose the capability; it does not constitute approval for every future high-impact tool invocation.

## Persistence and crash consistency
Schema migration v5 adds `agent_definitions`. Each `(agent_id, version)` is immutable. A unique partial index permits at most one active version per agent. Saving a draft persists the compiled approval requirements and highest risk in the same SQLite transaction as audit evidence. Activation atomically retires the prior active version, activates the reviewed version and appends audit evidence.

The repository refuses skipped/reused versions, activation of a modified definition, activation of a nonexistent/retired draft, and missing R4 approval.

## Acceptance evidence required before M6 credit
- full shared verification on Ubuntu and Windows for one exact PR head;
- schema migration regression from older databases through v5;
- unknown tool/model/schedule/resource references fail closed;
- risk mismatch fails closed;
- high-impact activation without approval fails closed;
- immutable-draft mutation is rejected;
- version replacement retires the previous active version atomically;
- Model Gateway drafting output is schema-validated and invalid output is rejected;
- no secrets, prompts or credentials are persisted by the Agent Builder tables/audit payloads.

M6 remains IMPLEMENTED/PREPARED, not GREEN/INTEGRATED, until the exact candidate passes these gates. HUMAN_TESTED and NVDA_VERIFIED are unrelated later human gates and are not inferred here.
