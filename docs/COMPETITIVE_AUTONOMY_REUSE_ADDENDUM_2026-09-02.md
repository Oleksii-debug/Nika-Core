# Nika Core — Competitive Autonomy & Reuse Addendum

Status: architecture clarification candidate for review; documentation-only.
Date: 2026-09-02.
Base policy: V0.1 remains the only active release target until sealed. Future horizons do not award V0.1 implementation credit.

This addendum records the 2026-09-02 product direction: Nika must be an autonomous, self-diagnosing, self-improving control plane rather than a brittle macro recorder, and it should aggressively reuse maintained external ecosystems instead of recreating solved infrastructure.

## 1. Browser/application reconnaissance must be first-class

A complex website must not be treated as a fixed list of taught buttons/selectors.

The preferred execution stack is:

`goal -> native/API check -> semantic DOM/accessibility map -> read-only JS/CDP reconnaissance -> bounded site model -> optional vision grounding -> governed action -> observable verification -> experience record`

For unclear or changed sites, Nika may use controlled `page.evaluate()`/equivalent page-context JavaScript and DevTools/CDP instrumentation to inspect legitimate application state, including DOM structure, accessibility roles/names, forms, contenteditable regions, frames, shadow roots, route transitions, safe client state, console failures, and redacted network metadata.

This diagnostic layer is read-only by default. Any mutation that can create an external effect remains a normal governed Nika action with permission/risk/effect identity and verification. JS/CDP diagnostics may not be used to bypass authentication, CAPTCHA, anti-bot controls, provider limits, security controls, or to extract unrelated credentials/tokens.

Nika should maintain bounded versioned Site Models for recurring environments so successful diagnostics become reusable knowledge rather than repeated from zero. A stale model is a hint, not authority.

## 2. Discovery-first research

The user should normally specify the research objective, constraints and expected coverage, not manually enumerate thousands of sources.

Professional discovery pipeline:

`goal -> seed/query generation -> search/API/directory/sitemap/link discovery -> candidate source graph -> validation -> quality/relevance scoring -> deduplication -> country/language/domain coverage accounting -> crawl/monitor queue -> evidence -> gap-driven rediscovery`

Large-source examples such as 10,000 sources are workload classes, not a requirement that the user personally provide 10,000 URLs.

V0.1 uses bounded controlled fixtures to prove execution mechanics. V0.2 is the first release expected to provide large-scale autonomous discovery/coverage as a product capability.

## 3. Quiet autonomy and management by exception

Normal internal difficulty is not a user-facing event.

Nika should silently manage ordinary retries, diagnostics, strategy changes, connector health checks, adapter repair attempts, experiment scheduling and internal maintenance. The default UI should expose outcomes and concise operational states, not engineering narration.

Interrupt the user only for a material exception such as:
- new credential/account authorization;
- destructive/high-impact approval;
- financial/contract authority;
- legal/licensing/privacy decision;
- changed deadline/SLA/resource/cost ceiling;
- unresolved safety boundary;
- persistent degradation that changes the promised result.

Detailed internals remain available in an expert diagnostics/audit view.

## 4. Always-on Experience and Improvement system

Every governed task should produce a privacy-bounded Experience record containing useful operational facts: environment/version, strategy/tool used, latency/resource cost, retry/recovery category, UI generation change, ambiguity, verification quality, safe failure class and whether a later strategy improved the result.

The system should continuously aggregate repeated pain points and opportunities into an Improvement Backlog.

Idle improvement modes:
- INTERACTIVE: background improvement minimal;
- BALANCED: conservative spare CPU/RAM;
- AWAY: heavier work when user declares/is safely inferred absent;
- OVERNIGHT_HEAVY: explicit schedule permits high utilization;
- BATTERY_ECONOMY: nonessential work suspended;
- CUSTOM: explicit CPU/RAM/GPU/thermal/time budgets.

Foreground tasks preempt improvement jobs. Background jobs checkpoint and yield when the user returns or resources become constrained.

Internet loss does not disable learning: Deterministic Brain, local models, local corpus, local replay/tests/benchmarks/coding sandboxes and offline indexes remain usable.

## 5. Controlled self-improvement, not uncontrolled self-modification

Required loop:

`Experience -> pattern detection -> hypothesis -> candidate prompt/strategy/tool/adapter/code -> isolated clone/branch/worktree/sandbox -> replay/regression/security/accessibility tests -> independent evaluation -> versioned promotion -> runtime monitoring -> rollback on regression`

Nika may create experimental copies of itself and compare them against current behavior, but may not silently make an experimental copy authoritative, widen permissions, bypass audit/approval, edit live production source in place, or rewrite historical evidence.

Version placement:
- V0.1: capability/health inventory, resource awareness, Experience Ledger foundation;
- V0.2: reusable environment/site/workflow experience, idle replay/benchmarking, offline improvement jobs;
- V0.3: full controlled Toolsmith/self-improvement promotion/rollback loop;
- V0.4+: self-improvement of Product Factory planning, staffing, coding, QA, deployment and maintenance strategies.

Do not claim literal consciousness/sentience. The engineering target is a persistent technical Self Model: capabilities, reliability by task class, current tools/models/providers, commitments, resources, confidence, recurring failures, experiments and software/release identity.

## 6. Reuse/interoperability fabric

Preferred integration order:
1. native application/provider API/SDK;
2. maintained standard protocol;
3. MCP tool/resource integration;
4. A2A-style remote agent interoperability;
5. Nika plugin/workspace adapter;
6. semantic UI automation;
7. vision/coordinates last.

A Connector Registry should track capability, version, authentication, permission/risk class, health, privacy class, cost/rate constraints, operations and fallback provider.

High-value first-party connectors include GitHub, Google Drive/Docs/Sheets, Gmail/Calendar/Contacts, Microsoft 365, databases, storage/cloud providers and approved business systems. Credentials remain in Credential/Identity Broker; models receive opaque references rather than persistent plaintext secrets.

## 7. Competitive reuse benchmark — 2026-09-02

### Microsoft Copilot Studio Computer Use
Microsoft's current direction includes generally available computer-using agents for websites/desktop apps, credentials/governance, workflows and adaptation to changing interfaces. Nika should not recreate a lower-grade RPA layer.

Nika differentiation target:
- user-owned durable local state;
- cross-reboot personal autonomy;
- semantic + JS/CDP diagnostic + vision layered interaction;
- offline deterministic/local-model operation;
- Windows/NVDA-first accessibility;
- experience-driven self-improvement;
- Product Factory and local resource scheduling in the same control plane.

### Microsoft UFO² / UI-focused Windows agents
UFO demonstrates HostAgent/AppAgent separation and UIA/Win32 application automation. Reuse/evaluate the maintained Windows automation ideas behind Nika-owned ports instead of building generic Windows UI plumbing from scratch.

### Google ADK + A2A
Google's ecosystem demonstrates cross-language multi-agent composition and A2A capability discovery/task exchange. Nika should interoperate with remote specialist agents through a provider-neutral Nika port rather than force every agent into one language/framework.

Nika retains durable task/project truth, permissions, exact effects and recovery independently from any external agent framework.

### OpenAI Codex
Codex demonstrates parallel long-running coding agents, worktrees/environments and always-on background engineering. Nika should treat Codex/OpenHands/other coding systems as replaceable CodingWorker resources and own the higher-level project organization, contracts, QA, release, credentials, cost policy and maintenance.

### BrowserCode / Browser Use
BrowserCode validates the user's proposed model of turning difficult browser interaction into a coding/diagnostic problem: runtime JavaScript over CDP, live browser state, reusable generated scripts. Nika should evaluate/reuse maintained Browser Use/Browser Harness concepts behind its own safety/effect contracts rather than reinvent unrestricted browser plumbing.

### MCP
MCP should be reused for compatible tools/resources/connectors rather than inventing another connector protocol. Nika still owns permission/effect/audit authority.

### Visual GUI agents such as UI-TARS
Vision-based computer use is valuable for inaccessible/canvas/visual-only interfaces. Nika should treat visual grounding as a fallback tier after native/API/semantic/diagnostic options, not as the first universal control method.

## 8. Product-quality floor

“Million-dollar-grade” is a design-quality floor, not a valuation promise.

Every claimed professional Nika capability should be judged by durability, long-horizon operation, recovery, concurrency, accessibility, least privilege, credential isolation, observability, rollback, versioned contracts, replaceability, cost/resource governance, independent QA, package provenance and realistic scale tests.

The external simplicity target is: user gives a difficult objective and gets the result; ordinary internal engineering remains Nika's responsibility.

## 9. Nika Voice / Persona horizon

Voice is a separate future ProductProject/profile over the same Nika Core, not a V0.1 release blocker.

Professional target:
- opt-in local wake phrase such as “Ніка”;
- far-field operation subject to microphone/acoustic quality;
- local VAD/keyword spotting before cloud use;
- streaming STT;
- low-latency full-duplex conversation with interruption/barge-in;
- expressive configurable neural TTS/persona;
- persistent user-approved memory and current task/project context;
- status queries, task creation, reprioritization and ordinary conversation without opening a dedicated voice screen;
- same R0-R4/approval boundaries as text.

Reuse candidates include sherpa-onnx for local keyword spotting/VAD/ASR/TTS/speaker features, measured local Whisper-class STT, local TTS engines where quality/license is acceptable, and optional cloud live-audio providers behind stable Voice ports.

OpenAI and Google now expose realtime native-audio/voice model surfaces; these should be optional providers, not the identity of Nika Voice.

Always-listening wake detection must be explicitly opt-in, visible, disableable and local by default. Ambient audio must not be continuously streamed to a cloud provider just to detect a wake phrase.

## 10. Android Nika Relay horizon

The Android companion should initially be thin:
- optional local wake phrase subject to Android background-microphone rules;
- authenticated encrypted pairing with home/desktop Nika;
- send voice/text notes/goals/priority changes;
- receive concise status and important exceptions;
- queue notes while home Nika is unreachable and synchronize later;
- revoke device access;
- never expose the desktop SQLite DB or credential vault directly.

Desktop/home/server Nika remains the durable control plane unless a later architecture deliberately promotes mobile/edge nodes.

## 11. Industrial / physical execution horizon

Industrial execution is post-V1/high-assurance work.

Candidate reuse ecosystems include OPC UA, ROS 2, vendor APIs/SDKs and domain adapters such as MQTT/Modbus where justified.

Safety separation is mandatory:

`Nika planning/supervision -> validated industrial intent -> simulation/digital twin -> approved adapter -> certified PLC/safety controller/interlocks -> physical machine`

An LLM/agent never replaces emergency stop, certified interlock, speed/force/rate limits, geofencing, safe-state logic or operator authority.

## 12. Architecture acceptance additions

Future acceptance should include:
- adaptive UI redesign recovery across controlled page generations;
- JS/CDP diagnostic canary/redaction and no mutation-authority bypass;
- discovery-first research where Nika finds the controlled source universe itself;
- quiet-autonomy management-by-exception behavior;
- Experience Ledger -> idle challenger -> held-out evaluation -> versioned promotion/rollback;
- offline improvement while cloud-dependent work waits durably;
- resource preemption when foreground work returns;
- MCP + remote-agent interoperability without provider framework leakage;
- future voice privacy/wake/barge-in gates;
- industrial simulation/interlock gates before any physical execution claim.

HUMAN_TESTED and NVDA_VERIFIED remain human-only.