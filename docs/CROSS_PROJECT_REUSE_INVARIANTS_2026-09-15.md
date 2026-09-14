# Nika Core — Cross-Project Reuse Invariants — 2026-09-15

Status: appendix to `docs/OPEN_SOURCE_ACCELERATION_PLAN_2026-09-15.md` inside the same #841 policy lineage. This is **not** a second roadmap, scheduler, runtime, backlog, shared framework, or assignment authority.

This appendix records only cross-project lessons that materially reduce `TIME_TO_FINISHED_NIKA` or future maintenance. Live `main`, #553/#803, current ownership, exact PR heads, current Actions and binding product specifications remain authoritative.

## 1. Source truth is a product invariant

Current Nika `main` is still mechanically unprotected (`protected=false`, required-status enforcement off). Existing Issue #450 owns the repository-settings fix. Do not open a duplicate source-level pseudo-guard.

Rules:

- Git must remain reconstructable product truth; Drive, ZIPs, reports and local snapshots are evidence/mirrors, never a higher source authority than the qualified Git lineage.
- A releasable candidate must bind exact source SHA/tree to build inputs and produced artifacts.
- Workers must not recover a modern product by archaeology across unrelated ZIPs/branches when a canonical Git lineage can be converged.
- #450 must eventually make ordinary direct/unreviewed `main` writes mechanically impossible and require the canonical checks.

## 2. Nika and Autopilot are complementary, not one runtime

Do not merge Nika-Core and ChatGPT Autopilot into one repository/runtime.

Boundary:

- Nika owns global project/task intent, priorities, dependency graph, worker selection, ownership/conflict keys, model policy, audit, Product Factory, exact-head review assignment, release selection and artifact/evidence verification.
- Autopilot owns reliable ChatGPT-Web execution mechanics: browser sessions/tabs, conversation reuse, prompt/send verification, busy detection, rate limiting, browser-local pause/stop, ambiguous-send reconciliation and browser restart recovery.
- Nika must not learn ChatGPT DOM details.
- Autopilot must not become global roadmap/product authority.

The interoperability target is a **small versioned contract**, not a shared agent framework. Reuse existing Nika task/worker concepts where possible. The minimum shape should be equivalent to:

`TaskEnvelope = project/task identity + priority + ownership key + dependencies + retry/cancellation + expiry + evidence requirements + capability requirements`

and

`WorkerResult = task/worker identity + terminal state + evidence refs + artifact refs + exact source/candidate identity + start/finish timestamps`.

Do not publish a separate shared package until at least three real consumers prove the contract is stable enough to justify one.

## 3. Generalize existing ToolEffectGuard; do not create an effect ledger #2

Autopilot's strongest reliability lesson is `VERIFY_BEFORE_RETRY` after an ambiguous side effect. Nika already has the correct foundation in `tools.py`: `ToolAuthorization`, `ToolEffectGuard`, `IdempotencyLedger`, completed-result replay and `UNCERTAIN` fail-closed behavior.

Therefore the direction is **ADAPT existing code**, not a new ToolInvocation subsystem:

- extend the future canonical capability projection with explicit idempotency/reconciliation semantics;
- effectful operations should declare a reconcile strategy such as provider idempotency, search/read-after-write verification, deterministic local state verification, or manual review;
- an `UNCERTAIN` effect is not automatically retryable;
- retry is permitted only after Nika can prove the previous effect did not happen or the provider's own idempotency key makes repetition safe;
- if Nika can prove the effect happened, record/complete it without re-effecting;
- if neither can be proved, remain fail-closed / manual-review rather than blind resend.

This applies to GitHub writes, Drive edits/uploads, email sends, file moves, installer mutations, browser submissions and Windows actions.

## 4. Capability risk must be two-dimensional

Nika already has effect-oriented `ToolRisk`. Keep it; do not replace permission authority. But the Capability Registry projection should distinguish **effect risk** from **data sensitivity** because a read can be side-effect-free and still expose highly sensitive information.

Minimum conceptual axes:

### Effect risk

- read-only / no external mutation;
- reversible local write;
- scoped reversible external write;
- communication, destructive or privileged action;
- money/legal/security/account-critical or effectively irreversible action.

### Data sensitivity

- public;
- internal/private;
- confidential/personal;
- secret/credential.

Capability metadata should then bind the existing Nika policy to exact domain/path/account/data scopes, credential references, approval mode, concurrency key, timeout, idempotency/reconciliation strategy and audit redaction rules.

The model may request a capability. It never grants itself a stronger risk/data scope.

## 5. One browser target, one effect authority at a time

Nika Playwright/general-browser work and Autopilot managed ChatGPT execution can coexist, but they must not concurrently control the same physical browser target.

When cross-product browser integration becomes active, reuse/adapt existing ownership concepts into one target lease/claim contract containing at least:

- browser profile/session identity;
- window/tab/conversation target identity;
- owning task/invocation;
- active provider (`autopilot-managed-chat`, `nika-playwright`, etc.);
- expiry/heartbeat/reconciliation semantics.

This is not another scheduler. It is an ownership fence preventing two effect authorities from racing on one target.

## 6. Reduce hourly-worker read cost with a derived coordination snapshot

The ecosystem already proved that thousands of issue comments are a poor machine message bus. Do **not** create a second backlog or truth database to fix that.

Allowed optimization:

- generate one compact, hash-addressed/read-only coordination snapshot derived from canonical live GitHub + Nika durable state;
- include current main, active ownership keys, canonical PR/head, dependency blockers, required gates and a small next-actions set;
- workers may use it to avoid repeated full history/pagination scans;
- immediately before any mutation/integration, perform an exact live late-bind/race guard against GitHub/current authority;
- snapshot mismatch means refresh/yield, never overwrite live truth.

The snapshot is a cache/projection, not assignment authority.

## 7. Shared surfaces: contracts/workflows first, not a shared god-package

Cross-project reuse is justified only in thin surfaces:

### Agent contracts

Versioned task/worker/evidence/artifact/ownership/retry/cancellation/terminal-reason schemas.

### Accessibility contracts

Keyboard path, focus invariant, semantic status, ARIA acceptance, UIA acceptance, diagnostics and a separately represented human NVDA acceptance state.

### Release evidence

Source identity, build identity, artifact manifest/hash, dependency manifest, test evidence, accessibility evidence and provenance.

### Reusable GitHub workflows

Prefer reusable workflow logic for Python/Node CI, Windows package, accessibility, release evidence and dependency audit over a new cross-project application framework.

Do **not** prematurely share browser runtime, retry implementation, logging framework, config framework, database implementation, installer/updater runtime or MCP implementation. Use maintained upstream solutions and thin product adapters.

Publish/extract a shared package only after repeated real consumers demonstrate stable semantics; before that keep schemas/workflows close to their canonical owner.

## 8. Dependency adoption requires exact provenance

For every new upstream engine/library used in Nika or PF11-generated software, capture at pin time:

- exact version/tag/commit;
- upstream source identity;
- package/wheel/archive hash where applicable;
- exact license from the pinned artifact/source, not memory about the project name;
- redistribution/notices obligations;
- supported Python/Windows/runtime constraints;
- security/update notes needed for maintenance.

This composes with the existing #695 SBOM/provenance lineage; do not build a second supply-chain framework.

## 9. Accessibility evidence must preserve the machine-vs-human boundary

Accessible Chess demonstrated a critical ecosystem invariant: correct DOM/UIA semantics and automated green checks can still fail the actual NVDA user workflow.

Therefore Nika must preserve distinct states:

`AUTOMATED_SEMANTIC_GREEN != HUMAN_TESTED != NVDA_VERIFIED`.

Automated gates should catch keyboard/focus/semantic/status/copyability regressions before owner testing. Physical NVDA remains an exact-candidate human gate and must never be inferred from UIA, DOM, screenshot or synthetic tests.

## 10. Testing: add property/fault methods where they buy real confidence

Autosport's strongest transferable testing pattern is not "more unit tests" but stateful/fault testing of durable invariants.

Use property/state-machine/fault tests selectively for Nika's highest-risk stateful boundaries:

- Product Factory lifecycle/review/promotion;
- idempotency and effect reconciliation;
- installer/update/rollback;
- durable task/restart/recovery;
- malformed external DTO/config ingestion;
- exact ownership/lease transitions.

Hypothesis or equivalent maintained tooling is appropriate when it finds state combinations that hand-written examples miss. Do not turn this into a test-count target or blanket dependency requirement.

## 11. Metrics that expose real acceleration

In addition to `TIME_TO_FINISHED_NIKA` / `DISTANCE_TO_FINISHED_NIKA`, coordinators may track only a small set of operational metrics when measurable:

- `TIME_FROM_DEFECT_TO_VERIFIED_RELEASE`;
- `OWNER_TIME_PER_WEEK` required for coordination/release work;
- `DUPLICATE_AUTHORITY_COUNT` / duplicate platform code trend;
- worker wake-up read/arbitration cost;
- reviewed-candidate-to-integrated time;
- number of real packaged journeys completed after restart.

Do not optimize these at the expense of safety or product truth; they are diagnostics for wasted coordination/duplication.

## 12. What must NOT be imported from the other projects

- Do not import Autosport sports/domain/economic authority into Nika Core.
- Do not make 12-6 AI's full scientific governance mandatory for every ordinary Nika change; transfer exact-source/evidence/reproducibility discipline only where risk justifies it.
- Do not make Autopilot's scheduler/storage/browser runtime a replacement for Nika Core runtime; keep the execution-plane boundary.
- Do not revive Nika-agent as a third orchestration platform; preserve unique research/adapter lessons only.
- Do not create a cross-repository `oleksii-shared` god-project.
- Do not create release-evidence, accessibility, task, retry, credential, browser or tool frameworks merely because several repos use similar words. Extract contracts only when semantics are truly shared.

## 13. Priority impact on the current Nika queue

These findings do **not** displace the current product blockers. Order remains:

1. close active exact source blockers and guarded serial integration;
2. repair PF4 trusted independent-review authority (#802) and then continue SAME #307 PF11 lineage;
3. close the real CodingWorker/worktree/sandbox/reviewer/GitHub-delivery vertical;
4. finish Windows/NVDA prerequisites and release spine;
5. then implement the cross-project contract/coordination optimizations above where they remove measured duplication/read cost.

The only immediate governance exception is existing #450: `main` protection is already a real proven repository-integrity defect and should be configured through repository settings without creating a competing source PR.