# Agent Lab activated-definition boundary

Status: generic Agent Lab platform invariant.

## Problem closed

Agent Builder, multi-agent orchestration and controlled experiments are separate Nika-owned subsystems, but execution must preserve one lifecycle truth: a team may run only an exact Agent Builder definition that is currently active. Naming an arbitrary agent ID/version must never bypass draft review, high-impact activation approval, retirement or the definition's declared tool grants.

## Binding invariants

1. A disabled `AgentDefinition` may be stored as a draft but may not be activated.
2. Multi-agent fan-out requires the parent definition and every requested child definition to exist and be the exact active version.
3. Draft, retired, disabled and nonexistent definitions fail closed before any child is spawned.
4. A child's runtime grants must be a subset of both the parent's effective grants and the child's own activated definition. Parent privilege is not permission to widen a narrower child definition.
5. Duplicate child member IDs and duplicate child thread IDs fail before partial fan-out persistence.
6. One child runtime exception is contained as a failed child result rather than corrupting sibling execution. Cancellation remains a distinct propagation path.
7. The runtime receives only the already-attenuated grants persisted for that team member.

## Architecture

This is CUSTOM (thin) Nika policy. No third-party framework can decide whether a Nika Agent Builder document is approved, active, retired, enabled or permission-compatible. LangGraph remains behind `AgentRuntimePort`; Pydantic remains the definition-validation layer. No new dependency is introduced by this boundary.

## Evidence

Regression coverage binds Agent Builder activation to M7 fan-out and exercises disabled activation, exact active-version enforcement, child-definition privilege narrowing, duplicate fan-out identity rejection, bounded parallel execution and arbitrary worker-exception containment.

This automated evidence does not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
