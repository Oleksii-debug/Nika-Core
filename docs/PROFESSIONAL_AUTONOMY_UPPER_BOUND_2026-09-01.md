# Nika Core — Professional Autonomy Upper Bound

Status: architecture clarification candidate for review; documentation-only.
Date: 2026-09-01.
Base policy: V0.1 remains the only active release target until sealed. Future horizons in this document do not award V0.1 implementation credit.

This document clarifies the professional product ceiling for Nika Core. It does not weaken any existing safety, accessibility, approval, exact-effect, restart, packaging or release gate.

Companion clarification: `docs/AUTONOMY_RUNTIME_AND_HORIZONS_2026-09-01.md` defines the Site Reconnaissance/JS-CDP diagnostic plane, discovery-first research, quiet autonomy, Experience Ledger, idle/offline improvement, persistent technical Self Model, MCP/A2A integration fabric, competitive architecture benchmark, Nika Voice/Persona, Android relay, industrial/physical execution horizon and proposed acceptance extensions.

## 1. Product value floor

Nika is not intended to be a macro recorder, brittle browser extension, toy three-agent demo, prompt wrapper or one-model chatbot.

The professional target is a durable autonomy control plane that can execute difficult multi-step work for days/weeks/months, recover from interruption, use several forms of intelligence, coordinate specialists, acquire missing capabilities under policy, preserve evidence and produce verifiable outcomes.

“Million-dollar-grade” is an engineering-quality metaphor, not a valuation claim. It means the minimum architecture should be judged against enterprise durability, observability, security, recovery, accessibility, replaceability, scale, testability and maintainability rather than against toy demonstrations.

## 2. V0.1 interaction must be adaptive, not selector-bound

The V0.1 browser/Windows interaction model must not assume a previously taught selector or coordinate is permanently correct.

Required reasoning pattern:

`observe current environment -> build semantic model -> select candidate action -> deterministic ambiguity/permission validation -> act -> verify observable effect -> record evidence`

Primary perception/action order remains:
1. native/API;
2. DOM/accessibility/UIA semantics;
3. deterministic named controls;
4. diagnostic page/browser instrumentation when ordinary semantics are insufficient;
5. vision/OCR grounding;
6. coordinates last.

If an interface changes, Nika should re-observe and re-resolve rather than blindly replay stale UI identity. Stale/ambiguous/high-impact actions fail closed.

The companion autonomy document extends this with controlled JavaScript/CDP diagnostics and Site Models. Those mechanisms are for understanding legitimate application state, not for bypassing authentication, CAPTCHA, anti-bot, rate-limit or security controls.

## 3. V0.1 agent-count contract

The official V0.1 acceptance fixture uses a real three-agent representative team because it is a bounded, testable minimum proof of multi-agent operation.

This is not a permanent product ceiling.

V0.1 architecture must remain compatible with bounded additional agents/roles where resource policy and task structure justify them. An advanced user should be able to request extra roles without creating a second orchestration framework.

Agent count is constrained by resource, concurrency, permission and task policy rather than by a hard-coded global “3 agents only” rule.

## 4. Configuration UX — conversation first, structured profile underneath

The primary V0.1 configuration experience should be conversational.

The user describes the job in natural language. Nika drafts a validated Task Profile and asks only for missing/material decisions such as credentials, high-impact permissions, source constraints, deadline or model/privacy policy.

Three views of the same canonical profile are expected:

### A. Conversational setup
Natural-language description + targeted clarifying questions.

### B. Advanced editor
Structured expert configuration for team, models, tools, schedule, retry/recovery, resources, outputs, approvals and limits.

### C. Portable Task Profile
A versioned export/import artifact such as `.nika-task.json` that can be drafted with another approved AI/tool and imported into Nika after validation.

The portable profile never stores plaintext persistent secrets. It references Credential/Identity Broker entries.

## 5. V0.1 professional upper-bound workload

A valid V0.1 stress target is not “open one page and click one button.”

Representative upper-bound class:
- long-running multi-day operational task;
- hundreds of controlled/approved target operations over time while the deterministic acceptance fixture remains bounded;
- bounded concurrent browser work;
- real three-agent team plus optional bounded additional roles;
- browser and local-file work;
- mixed HTML/PDF/DOCX/XLSX/CSV/TXT inputs where existing parsers support them;
- HTTP/API-first retrieval with semantic browser fallback;
- semantic read/type/select/invoke actions;
- observable post-action verification;
- scheduled/recurring work;
- local or configured API intelligence through ModelGateway;
- network outage/reconnect;
- sleep/hibernate;
- application restart;
- Windows reboot with user-enabled autostart;
- no duplicate confirmed external effect;
- uncertain effect reconciliation;
- accessible per-target and team status;
- daily/final accessible reports.

V0.1 is successful only when these foundations operate as one packaged Windows product journey. It is not expected to contain the full future Toolsmith/Product Factory/Voice/Industrial stack.

## 6. V0.2 professional upper-bound — autonomous research/operations desk

V0.2 should be evaluated as a persistent research/operations capability, not a one-shot search toy.

Professional workload class:
- user supplies the objective and constraints rather than manually enumerating every source;
- Nika discovers, validates, ranks and deduplicates large source universes;
- thousands to 10k+ source-class research programs where infrastructure/resources allow;
- multiple countries/languages/source types;
- web/API/document acquisition;
- incremental crawling and change detection;
- provenance and evidence;
- local corpus/index;
- cheap deterministic filtering before expensive model calls;
- privacy-aware local/cloud routing;
- recurring operations for weeks/months;
- daily operational reports, periodic executive synthesis and immediate critical-change alerts;
- automatic degradation/recovery when individual sources fail;
- source/schema/UI adaptation without forcing the user to maintain every workflow manually.

The companion autonomy document defines discovery-first source expansion and coverage accounting explicitly.

## 7. V0.3 professional upper-bound — controlled capability acquisition

V0.3 is not justified by examples like “learn how to read an ordinary document format.” Standard capabilities should already reuse maintained libraries/adapters.

The meaningful class is a long-running job where real external systems change or a genuinely new capability is needed.

Required flow:

`running durable task -> concrete capability gap -> search existing Nika/upstream capability -> REUSE/ADAPT when possible -> isolated Toolsmith/Software Factory implementation only when needed -> tests/security/compatibility/accessibility -> independent evaluation -> versioned registration -> original task resumes from checkpoint`

Example classes include new vendor APIs, nonstandard documented data formats, changed authenticated workflows, new validation algorithms or new specialist adapters.

Internal capability acquisition should normally be quiet. The user should not be burdened with ordinary implementation details unless a credential, high-impact approval, legal/licensing decision, cost/resource change, deadline change or unresolvable policy boundary requires them.

## 8. Continuous evidence-driven self-improvement

Nika should learn operationally from repeated work rather than reset to zero after every task.

The approved model is continuous evidence-driven improvement, not uncontrolled live self-modification.

Required loop:

`experience -> failure/opportunity pattern -> challenger strategy/tool/prompt/adapter/code -> isolated experiment -> replay/held-out metrics -> independent evaluation -> versioned promotion -> production monitoring -> rollback on regression`

Areas may include:
- task decomposition;
- team composition;
- tool/model/provider routing;
- browser semantics and diagnostics;
- source ranking/dedup;
- prompt/planning strategies;
- retry/recovery behavior within canonical safety rules;
- coding/testing strategies;
- resource scheduling.

The companion autonomy document defines the Experience Ledger, Idle Improvement Engine, offline improvement and persistent technical Self Model.

## 9. Large ProductProjects require hierarchy, not a flat swarm

For V0.4+ Product Factory, a professional project should model an organization rather than treat 20/100 workers as undifferentiated peers.

Representative hierarchy:

`User/Product Authority -> ProductProject Director -> Workstream Leads -> Component Leads -> Workers`

Independent QA/security/accessibility/release roles remain organizationally separate from implementation ownership.

Large projects can add/remove specializations based on scope and risk without widening the project permission ceiling.

ProductRepositoryGraph provides one/multi-repository component ownership and dependency truth. Shared contracts require explicit compatibility decisions.

## 10. Professional Product Factory workload classes

Product Factory should eventually be capable of managing product programs at classes such as:

### Social-network-class product
Independent product with web/mobile/backend/identity/social graph/feed/media/search/notifications/moderation/analytics/infrastructure/accessibility/security/release components.

### Messenger-class product
Independent product informed by public competitor research, with identity/sync/storage/attachments/calls/push/desktop/mobile/accessibility/abuse-prevention/backend/infrastructure/release workstreams.

### AI mail/client platform
Multi-account mail protocols/authentication, MIME, attachment handling, offline sync, indexing/search, rules, local/cloud AI policy, accessibility, migration/update/recovery.

### Model-engineering/training program
Data provenance/dedup/tokenization, training configuration, local/remote GPU execution, checkpoints, experiment/evaluation datasets, registry, serving, quantization and release evidence.

These are workload classes, not products hard-coded into Nika Core. Duration and compute scale may be weeks/months and may require remote execution infrastructure.

## 11. External coding/reasoning workers

Nika should be able to use strong external coding workers as replaceable resources rather than forcing a small local model to perform frontier-level coding.

Potential supported channels include:
- Codex through supported product/API/CLI/connectors;
- OpenHands behind CodingWorkerPort;
- future approved coding workers;
- local coding workers;
- remote execution nodes.

Nika owns:
- project decomposition;
- context/work-package generation;
- role/ownership allocation;
- repository/worktree/branch identity;
- tests/CI/evidence;
- independent review;
- integration/release truth.

External workers do not own Nika's permissions, ProductProject state or release authority.

Browser interaction with external AI products may be supported only where ordinary use/automation is permitted. It must not be deliberately designed to bypass provider billing, usage limits, anti-abuse controls or unsupported automation restrictions.

## 12. Version placement summary

### V0.1
Durable autonomous operator foundation: packaged Windows journey, real multi-agent work, semantic/adaptive browser operation, timing, monitoring, local/API AI, continuity, autostart, exact effects, experience/resource-health foundations.

### V0.2
Persistent autonomous research/operations platform: discovery-first large-source research, corpus, richer monitoring, environment experience, idle/offline replay and broader connectors.

### V0.3
Controlled self-improving capability platform: Toolsmith, isolated capability/self-improvement candidates, experiment-driven promotion/rollback, mature Self Model.

### V0.4
Autonomous Product Factory: dynamic hierarchical organizations, multi-repository products, coding workers, remote nodes, deployment and maintenance.

### V0.5/V1.0
Expanded intelligence/labs/business/media/model capabilities and then stable integrated product acceptance for the selected release scope.

Post-V1 optional horizons include Nika Voice/Persona, a thin Android relay, and industrial/physical execution under high-assurance safety boundaries. See the companion autonomy document.

## 13. Acceptance principle

Professional claims are accepted only through executable gates on one exact integrated product path.

A feature list, model demo, generated source tree, worker count or green sibling PR set is not proof of the claimed professional workload.

The product must preserve the existing truth states:
- IMPLEMENTED;
- GREEN;
- INTEGRATED;
- PACKAGED;
- HUMAN_TESTED;
- NVDA_VERIFIED.

Future acceptance additions proposed in the companion document include adaptive UI recovery, diagnostic safety, discovery-first research, quiet autonomy, experience/improvement, offline improvement, resource preemption and interoperability gates.

HUMAN_TESTED and NVDA_VERIFIED remain human-only.