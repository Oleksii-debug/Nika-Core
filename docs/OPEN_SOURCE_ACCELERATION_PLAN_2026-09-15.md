# Nika Core — Free/Open-Source Acceleration Plan — 2026-09-15

Status: proposed binding execution overlay for autonomous workers after merge.

This document does **not** create a second roadmap, scheduler, runtime, Product Factory, Model Gateway, memory authority, browser authority, permission system, or coordination queue. It narrows execution toward the shortest path from the current live product to a useful, testable Windows/NVDA Nika and a real Product Factory vertical slice.

Canonical live coordination remains Issue #553 with Issue #803 as the writable overflow transport. `AGENTS.md`, `docs/AUTONOMOUS_WORKER_ORCHESTRATION.md`, binding product specifications, current `main`, current PR heads, current GitHub Actions and current ownership always outrank stale reports.

## 1. Owner directive: FREE / OPEN FIRST

Until the owner explicitly changes this policy, implementation priority is:

1. deterministic/model-free execution;
2. existing code already in Nika;
3. maintained open-source components that can run locally;
4. free local models and local inference servers;
5. genuinely free cloud/free-tier resources when they are optional and do not create lock-in;
6. paid services are **out of active implementation scope** for now.

Workers must not block completion on purchasing API credits, subscriptions, paid sandboxes, paid model access, paid SaaS, or paid data. A paid option may be mentioned only as a future alternative in a short note if it gives an unusually large multiplier. Do not build the current architecture around it.

## 2. Product objective

Optimize only:

- `TIME_TO_FINISHED_NIKA`;
- `DISTANCE_TO_FINISHED_NIKA`;
- number of real packaged user journeys that work after restart;
- number of critical end-to-end gaps removed.

Do **not** optimize PR count, commit count, issue count, audit length, worker count, abstraction count, test count in isolation, or amount of newly written code.

The next major proof is not another architecture document. It is a real vertical slice:

`natural-language product request -> durable ProductProject -> research/requirements -> real repository/workspace -> real coding worker -> tests -> independent review -> repair if needed -> package/artifact -> accessible delivery -> restart/reopen/provenance`.

For the ordinary Nika Windows product, the parallel target is:

`install/start -> keyboard/NVDA UI -> configure allowed model path -> create task -> agents execute -> approvals where required -> visible progress/results/errors -> pause/stop/restart/recovery -> durable history`.

## 3. Architectural authorities that are frozen

Do not create competitors to these authorities unless a measured, documented failure proves the current contract cannot satisfy the requirement:

- agent execution authority: `AgentRuntimePort`, with LangGraph as the current production adapter;
- scheduler authority: existing Nika durable scheduler / `SchedulerPort` path;
- permissions and approvals: existing Nika policy/approval authority;
- model authority: Nika `ModelGateway` and Nika model/intelligence policy;
- Product Factory authority: current ProductProject/Product Factory domain and program host;
- coding-engine boundary: `CodingWorkerPort`;
- browser orchestration: current interaction subsystem with Playwright foundation;
- Windows semantic interaction: UIA-first path;
- memory authority: current durable Nika memory/state services;
- research authority: Universal Research;
- capability/tool authority: evolve existing tools/MCP/plugins/Toolsmith surfaces into one canonical capability projection; do not create a separate tool ecosystem.

External components are adapters behind these boundaries, not replacements for the Nika domain.

## 4. Reuse strategy: what to integrate instead of rewriting

### Tier A — highest leverage now

**OpenHands Software Agent SDK / Agent Server**

Use as a candidate real coding engine **behind `CodingWorkerPort`**. Nika keeps job identity, base SHA, allowed paths, permission ceiling, resource/network policy, acceptance commands, recovery, review and promotion. OpenHands may understand/edit code and run permitted commands inside the assigned workspace. It must not become a second Nika runtime or Product Factory.

Requirement: prefer a configuration that can use local/open/free model backends. Integration work must remain useful even when no paid API exists.

**git worktree**

Use one isolated worktree per real coding job, bound to exact base SHA and candidate identity. Do not invent another workspace format when Git can provide the source-level isolation primitive.

**real sandbox tier**

Metadata named `PROCESS_CONTAINED` is not a security sandbox. For untrusted generated code, require an actual isolation level (`OS_SANDBOXED` or `REMOTE_SANDBOXED` equivalent) before execution. Prefer open/local mechanisms that are already available on the target machine; do not make paid remote sandboxes a requirement.

**independent reviewer execution**

Author and reviewer must be separate execution identities/contexts over immutable candidate/base evidence. A worker cannot self-assert that its own change was independently reviewed. Promotion remains Product Factory authority.

**GitHub adapter / official GitHub MCP where appropriate**

Reuse maintained GitHub API/MCP capability for repository/branch/PR/check operations, but keep credentials, permission decisions, audit, accepted base/head identity and promotion policy in Nika.

### Tier B — high leverage after the PF11 execution gap is moving

**Capability Registry / Tool Broker consolidation**

Do not create a new tool universe. Consolidate the metadata already represented by `tools.py`, Action Registry, MCP boundary, Plugin SDK and Toolsmith into one canonical projection with capability ID/version/source/license/risk/effect class/allowed data/filesystem/network/account scope/required permission/approval mode/health/audit metadata.

The official MCP Registry is discovery metadata, not a trust authority. No uncontrolled auto-installation.

**Playwright / Playwright MCP**

Keep Playwright as the browser foundation. Agent-facing Playwright MCP capability belongs behind Nika's Tool Broker and permission/effect policy. Do not create a second browser scheduler.

**Windows UI Automation + pywinauto**

Keep UIA semantic properties and patterns as the primary desktop path. Vision/OCR/coordinates are fallbacks, not the default. This is particularly important for keyboard/NVDA reliability.

**Microsoft UFO family**

Treat as an optional higher-level Windows semantic-interaction adapter/benchmark candidate, never as a replacement for Nika `AgentRuntimePort`, permission authority or recovery semantics. Prototype only after the deterministic UIA path has a stable action contract and measure it on the same tasks.

### Tier C — replace low-level utility work, not Nika domain logic

**LiteLLM**

Use only as provider normalization behind `ModelGateway` if/when it materially reduces adapter maintenance. Nika continues to decide privacy, eligibility, budgets, provider class, health, fallback and escalation.

**Ollama / llama.cpp / Foundry Local**

All are model backends, not domain authorities. Current active policy is free/local first. Do not make any one backend the only way Nika intelligence works.

**Docling / Apache Tika / existing format libraries**

Universal Research should own source identity, evidence, cutoff/freshness, deduplication, citations, scheduling and durability. External libraries should own PDF/Office/general format extraction where they outperform current thin parsers. Measure on a fixed corpus before replacing a working path.

**Tesseract / PaddleOCR** and **faster-whisper / whisper.cpp**

Use as optional local OCR/transcription engines when needed. Do not write OCR or speech-recognition engines inside Nika.

**SQLite FTS5**

Use lexical/full-text retrieval first. Do not add Qdrant/Weaviate/Milvus or another vector service merely because the project uses AI. Add an embedding index only after a fixed benchmark demonstrates a real retrieval gap.

## 5. What workers must stop doing

The following work reduces delivery speed unless a current acceptance failure explicitly requires it:

- opening a new general agent framework lane (CrewAI, AutoGen, Semantic Kernel Agents, etc.) beside the current runtime;
- adding a second scheduler, memory authority, Model Gateway, Product Factory, permission system or browser control plane;
- writing custom inference servers, OCR engines, speech engines, generic PDF/Office parsers, generic browser engines or Git hosting clients when maintained components exist;
- widening Product Factory bookkeeping/history machinery without closing a real end-to-end acceptance gap;
- building the full Web/Cloud edition or Business Factory before the active Windows/PF11 vertical slices are proven;
- producing repeated broad audits with no source change, no new evidence and no concrete routing change;
- rereading all old Drive/GitHub history every hourly wake-up;
- creating successor PRs for the same unfinished canonical outcome instead of continuing the incumbent lineage;
- creating QA-only PRs for ordinary review findings that belong on the canonical PR;
- rerunning expensive full gates on unchanged SHAs without a concrete reason;
- rewriting stable ports/adapters merely to use a fashionable framework;
- treating a green unit test, class existence, document, mock, synthetic proof or `create/inspect/reopen` demo as packaged Product Factory completion;
- making paid APIs or subscriptions prerequisites for current progress;
- inventing `HUMAN_TESTED`, `NVDA_VERIFIED`, physical Windows, provider or hardware evidence.

## 6. Decision rule for every worker wake-up

Before modifying source:

1. refresh live `main`, current assignment owner, current canonical PR/head and applicable CI;
2. ask: **what is the first broken link in the nearest real packaged user journey?**;
3. ask whether an incumbent owner is already repairing it;
4. search existing Nika code and maintained upstream components before custom implementation;
5. choose the largest coherent non-colliding slice that moves the broken link toward real completion;
6. implement source + wiring + failure/recovery behavior + focused tests in the same lineage;
7. run the cheapest relevant checks first, then the required exact-head gates;
8. obtain independent review only after there is a stable candidate worth reviewing;
9. hand off concise exact evidence; do not produce status prose with no actionable change;
10. if blocked by a paid service, unavailable hardware or unavailable permission, route to a free/local/mockable compatible slice instead of idling.

## 7. Ranked next work packages

These are **ranked outcomes, not permission to open 30 simultaneous PRs**. Coordinators map them onto existing live owners and incumbent PRs. Existing ownership wins. Keep the repository WIP limit.

1. **PF11-REAL-REFERENCE-JOURNEY** — define one minimal, real reference product request whose success ends in a usable artifact, not a simulated state transition.
2. **CODING-WORKER-REAL-BACKEND** — finish one production-capable adapter behind `CodingWorkerPort`, preferring OpenHands or another maintained open engine after a focused compatibility proof.
3. **CODING-WORKTREE-IDENTITY** — bind every coding job to exact base SHA, worktree path, candidate commit/diff and cleanup/recovery semantics.
4. **SANDBOX-ENFORCEMENT** — make isolation classes enforced behavior; block untrusted generated code from weak containment tiers.
5. **CODING-RESULT-MANIFEST** — return exact commit, diff identity, commands, tests, artifacts and resource/effect summary.
6. **REVIEWER-AUTHORITY** — produce review evidence only from a separately launched reviewer job/service over immutable candidate/base.
7. **REVIEWER-SEPARATION-GATE** — prevent author identity/context from satisfying independent-review requirements.
8. **PROMOTION-GATE** — Product Factory alone promotes after verification + independent review + required approvals.
9. **GITHUB-DELIVERY-PATH** — prove repo/branch/commit/PR/check/status operations through the canonical GitHub capability without bypassing policy.
10. **CI-REPAIR-LOOP** — failed check -> structured evidence -> repair job -> new candidate -> fresh independent review/gates.
11. **PF11-PACKAGE-ARTIFACT** — generate/package the reference product with provenance bound to source/review/check identities.
12. **PF11-RESTART-RECOVERY** — kill/restart the program mid-project and prove deterministic recovery without chat memory.
13. **PF11-ACCESSIBILITY** — generated reference product receives its own keyboard/accessibility checks; do not inherit Nika's accessibility evidence.
14. **CAPABILITY-REGISTRY-SCHEMA** — consolidate current tool/MCP/plugin/Action Registry/Toolsmith metadata behind one canonical capability projection.
15. **CAPABILITY-RISK-GATE** — every executable capability records side-effect/risk/filesystem/network/account/data/approval scope.
16. **MCP-DISCOVERY-POLICY** — allow controlled discovery from the official registry; no auto-trust/auto-install.
17. **PLAYWRIGHT-MCP-ADAPTER** — expose agent browser capability through the Nika Tool Broker without creating another browser authority.
18. **WINDOWS-UIA-ACTION-CONTRACT** — formalize stable semantic locator/action/result identities for deterministic Windows automation.
19. **WINDOWS-UIA-BENCHMARK** — fixed keyboard/UIA task corpus with success, recovery and ambiguity evidence.
20. **UFO-ADAPTER-SPIKE** — only after 18/19: benchmark an isolated UFO-family adapter against the same Windows tasks; keep it only if it adds measured value.
21. **MODEL-ROUTING-POLICY** — complete capability/privacy/locality/health/latency/cost scoring while current policy keeps paid providers inactive.
22. **LOCAL-MODEL-LADDER** — deterministic -> embedded/local -> external local; cloud/free-tier only when explicitly eligible. Paid tier remains disabled.
23. **LITELLM-ADAPTER** — add only if provider normalization removes real duplicate adapter code; prove Nika policy cannot be bypassed.
24. **LLAMACPP-OPENAI-COMPAT** — optional provider adapter/health discovery, not a new model subsystem.
25. **RESEARCH-DOCLING-BENCHMARK** — compare current parsers vs Docling on a fixed lawful PDF/Office corpus with provenance retention.
26. **RESEARCH-FTS5-BENCHMARK** — measure lexical/metadata retrieval; vector index remains deferred until a real gap is demonstrated.
27. **A11Y-WORKFLOW-MATRIX** — every major Nika workflow has keyboard path + semantic DOM assertions + UIA mapping where applicable.
28. **NVDA-RELEASE-PROTOCOL** — preserve physical NVDA as a human release gate, but ensure all automated prerequisites finish before asking the owner to test.
29. **THIRD-PARTY-PROVENANCE** — dependency adoption records exact version/tag/commit, license, source, distribution obligations and security notes; prepare SBOM/notices automation.
30. **LICENSE-DECISION-PREP** — prepare owner-facing repository-license options and consequences, but do not choose or change the Nika-Core license without explicit owner approval.

## 8. Assignment guidance for the existing 13+2 topology

Do not create new scheduled workers merely for this plan. Coordinators map the ranked packages to current workers according to live ownership and collision risk.

Suggested home alignment only:

- DEV01/DEV02/DEV13: PF11 reference journey, ProductProject inputs, end-to-end composition.
- DEV03/DEV10: durable job/worktree/restart/recovery semantics.
- DEV04: GitHub delivery path and candidate/base identity.
- DEV05: permission/approval/capability risk boundaries and credential references.
- DEV06: real `CodingWorkerPort` backend and result manifest.
- DEV07: sandbox/worktree/process execution enforcement.
- DEV08: exact verification, CI repair evidence and redundant-gate reduction.
- DEV09: independent reviewer authority and adversarial acceptance.
- DEV11: packaging/artifact/update/rollback for Nika and PF11 reference artifact.
- DEV12: keyboard/NVDA UI, semantic workflows and user-visible status.
- COORD-A: priority, collision avoidance, integration queue, stale-branch convergence.
- COORD-B: usable Windows vertical slice and acceptance readiness.

A live ownership record overrides this suggested mapping.

## 9. Definition of useful progress

Count progress only when at least one of these becomes more true for an exact candidate:

- a real packaged journey advances through a previously broken link;
- a mock/synthetic path is replaced by a production-capable adapter;
- a manual/unsafe step becomes deterministic and governed;
- a restart/recovery gap is closed;
- a duplicate authority is removed or prevented;
- maintained upstream code replaces custom low-level maintenance burden without weakening contracts;
- a required gate becomes more truthful, cheaper or less redundant without weakening evidence;
- accessibility becomes available through a nonvisual path;
- an exact candidate is integrated and remains green under the actual combined main.

Everything else is supporting work and must justify why it is the shortest dependency path to one of the above.

## 10. Non-negotiable accessibility rule

The primary owner is blind and uses Windows 11 + NVDA. Every key capability must have a nonvisual path. Prefer semantic HTML/ARIA in the existing desktop WebView, UIA semantic interaction for Windows, accessible structured reports and keyboard operations. Coordinate clicks and screenshots are last-resort automation, not the product's primary control model.

Do not ask the owner to inspect code, branches, CI logs or visual UI to compensate for missing engineering. Human NVDA testing should occur only on a frozen, downloadable candidate after automated prerequisites are complete.

## 11. Adoption rule

After this policy is merged:

- `AGENTS.md` should point workers here for the owner-directed free/open-source acceleration overlay;
- `docs/AUTONOMOUS_WORKER_ORCHESTRATION.md` remains the worker-count/routing/integration authority;
- Issue #803 receives one concise activation record; it must not become a second backlog;
- Drive may mirror this policy for owner visibility, but Drive does not become assignment authority;
- no worker should reopen architectural choices that this document explicitly freezes unless it brings measured evidence of a concrete failure.
