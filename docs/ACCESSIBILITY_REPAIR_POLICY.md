# Accessibility Repair Policy

Status: MANUAL-DEV15 implementation contract.

## Ordering

Accessibility repair is evidence-driven and semantic-first:

1. browser DOM semantics or Windows UI Automation semantics;
2. OCR, when semantic inspection cannot identify an actionable target;
3. vision grounding, when OCR remains insufficient or ambiguous;
4. coordinate targeting only when semantic, OCR, and vision attempts have all failed policy and a coordinate adapter was explicitly configured.

A lower tier never erases the failure evidence from a higher tier. `FallbackAttempt` records the method, failure cause, confidence, and target revision without persisting screenshot contents.

## Fail-closed boundaries

Evidence is not actionable when the target is ambiguous, confidence is below the configured threshold, no named controls were resolved, or the inspected UI revision is missing. Visual evidence must be redacted before it crosses the policy boundary and token-shaped credential material is rejected from target, summary, revision, and control text.

A changed `target_revision` invalidates an action handoff and requires re-inspection. This prevents a stale OCR/vision/UIA result from being replayed after the interface changes.

## Action separation

`AccessibilityRepairService` does not execute UI actions. `prepare_action_handoff()` emits a deterministic evidence-bound handoff and always keeps `requires_approval=true`. Inaccessible UI never weakens ToolExecutor or approval policy.

Coordinate evidence can only produce a handoff when recorded failures prove semantic + OCR + vision exhaustion. Coordinate evidence cannot be converted into a reusable helper because coordinates are not durable semantic identity.

## Versioned helpers

`build_helper_spec()` emits `nika-accessibility-helper-v1` for a single named control bound to a target revision and evidence SHA-256. Helper identity is deterministic for the same evidence and control. A changed target revision requires new evidence and therefore a new helper identity.

## Automated evidence truth

The focused regression suite covers semantic short-circuiting, OCR-before-vision ordering, ambiguous-target recovery, low-confidence rejection, coordinate-last behavior, changed UI rejection, deterministic safe handoff/helper generation, credential-shaped text rejection, redaction enforcement, and strict candidate identity.

Automated tests do not set `HUMAN_TESTED` or `NVDA_VERIFIED`.
