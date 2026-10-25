# Nika Core — Universal Goal Operating System extension, 2026-09-11

## Binding intent
This document extends the Product VNext specification. It does not authorize a rewrite and does not displace the current Windows/Development-Factory finishing path. Existing canonical components MUST be reused, repaired and composed.

The target is broader than a coding agent or chat application: Nika is a persistent goal-execution system in which a long-lived mission/project, not a chat session, is the primary unit.

## 1. Universal missions and portfolio
A user may operate tens or hundreds of simultaneous missions across software development, business, research, learning, personal administration, document work, monitoring, automation and market analysis.

Each mission persists independently of any single model/session and owns: outcome, acceptance criteria, deadline, priority, dependencies, budget, autonomy policy, model/tool policy, permissions, human gates, team, work graph, decisions, artifacts, evidence and next-best action.

Nika must optimize the portfolio, not merely a single chat: allocate execution where it most reduces distance to accepted outcomes while respecting user priorities and isolation between projects.

## 2. Mission contract and autonomy
Every durable mission has a versioned machine-readable contract defining success, forbidden actions, authoritative data sources, required evidence, budgets, privacy/location policy, approvals, stop conditions and escalation conditions.

Autonomy is capability-scoped rather than one global switch. A mission may allow autonomous read/research, autonomous local reversible work, approval-gated external communications, and prohibit financial or destructive actions.

## 3. Event-driven and continuous execution
Missions can wake on user commands, schedules or events: mail, file changes, repository events, calendar events, metrics, prices, database changes, service health, completion of another task or human approval.

A trigger never bypasses policy. The mission is reconciled against current state before work is dispatched.

Continuous missions survive individual agent/context termination. Agents are replaceable workers; the mission is the durable owner of state.

## 4. Humans, agents, deterministic automation and external workers
Nika must orchestrate four execution classes in one graph:
- humans;
- Nika-native agents;
- deterministic tools/robots/workflows;
- external agents or agent services.

A human task must be short and actionable: why input is needed, what to do, available choices and what evidence/result to return. Independent work continues while waiting where safe.

External agents are contractors, never global authorities. They receive least-context/least-permission task packets and return artifacts/evidence for Nika to accept or reject.

## 5. Distributed compute and warm environments
Nika may execute across the current Windows PC, other user-owned PCs, home servers, local accelerators, approved cloud environments and external agent services.

Each execution endpoint has identity, capabilities, health, permissions, privacy zone and load.

Repeatable environments should support warm/prebuilt snapshots: repository/material already present, dependencies installed, caches prepared, tools validated and secrets injected only when required. Environment versions are auditable and reproducible.

## 6. Model and provider fabric
Model selection is policy-driven at portfolio/project/team/role/task level.

Nika must support concurrently:
- no-LLM deterministic execution;
- embedded/local models;
- external local model servers;
- multiple replicas of the same model;
- different local models;
- models on other private machines;
- approved API/cloud models;
- local and API routes at the same time.

No global current-model singleton or global inference mutex may serialize unrelated projects. Active work retains its bound provider/model identity unless explicitly migrated.

## 7. Universal artifact factory
Nika must produce and verify more than code: Windows applications, websites, documents, spreadsheets, presentations, archives, research reports, datasets, trained models, images, audio/video, grant packages, budgets, plans, calendars and dashboards.

Every material artifact has provenance, exact version/identity, acceptance criteria and validation evidence.

## 8. Business operating mode
A business mission may include a measurable outcome, budget and approval boundaries. Nika may gather permitted data, maintain KPIs, analyze funnels, propose and run allowed experiments, prepare communications/artifacts, assign human/agent work, track deadlines, compare plan vs actual and stop ineffective experiments.

Supported classes include sales, marketing, support, operations, procurement, budgeting, grants, contract/deadline tracking, HR workflows, internal knowledge and reporting.

Activity count is not success; accepted business outcome and evidence are the metrics.

## 9. Research mode
Research is a reproducible workflow: question -> protocol -> sources -> conflicting evidence -> analysis -> alternative explanations -> adversarial challenge -> verification -> conclusion with confidence.

Critical claims retain source/evidence lineage. A red-team role may intentionally attempt to falsify the leading conclusion before acceptance.

## 10. Market/finance mode
Nika may collect permitted market data/news, build scenarios, backtest rules, measure drawdown/risk, run paper portfolios and prepare decisions.

Real-money execution is a separately gated capability. Default rules: paper and live execution are strongly separated; live orders are disabled unless explicitly enabled; position/loss limits are immutable from inside the strategy; emergency stop exists; each order has decision/evidence provenance; strategies cannot silently raise their own risk limits; an independent execution guard validates live actions.

No product claim should imply guaranteed investment returns.

## 11. Learning and personal missions
Nika may maintain durable learning programs with diagnostic assessment, curriculum, spaced review, exercises, adaptive difficulty and cross-device progress.

Personal missions may cover travel, relocation, housing, paperwork, purchases, events and personal archives, subject to the same permission/privacy contract.

## 12. Communications and knowledge graph
Incoming/outgoing communications must attach to durable projects and decisions, avoiding duplicate replies from parallel agents.

Knowledge is not only a vector/full-text index. Nika should retain entities, relationships, versions, sources, contradictions, confidence, decisions and temporal validity so agents can retrieve the minimum relevant trustworthy state.

## 13. Monitoring and scenario simulation
Long-running monitors should emit work only on meaningful state transitions, not unchanged-state spam.

Before expensive/irreversible actions Nika may fork controlled scenarios (baseline/optimistic/pessimistic/alternative-agent proposals), compare expected outcome, risk, cost and reversibility, then choose or ask the user.

## 14. Permission, governance and risk core
Permission must be capability-scoped and time-bounded rather than granting an agent unrestricted account access.

Risk classes include read-only, local reversible mutation, external reversible mutation, publication, financial action, legal-significance action, destructive deletion, credential/access changes, secret access and infrastructure/physical effects.

Each class has an approval policy.

One emergency-stop command must prevent new external effects, cancel safely-cancellable actions, preserve evidence/state and support controlled recovery.

Untrusted webpages/files/messages are data, not policy. Prompt-injection content may not alter system rules, secrets or permissions.

## 15. Observability and anti-loop control
At portfolio/project level Nika reports accepted outcomes, rejection/rework rate, time-to-accepted-result, cost, model/tool use, errors, human escalations, duplication and bottlenecks.

The primary optimization metric is distance to accepted outcome, not token count, number of agents, commits or messages.

Nika must detect loops: repeated unchanged audits, successor churn, revisiting rejected options without new evidence, comment storms, agents undoing each other or repeated no-op work. Such lanes are frozen and resources are reassigned.

## 16. Evidence and independent qualification
Agent self-report is never sufficient for critical completion. Qualification may require deterministic tests, independent agent/model review, external validators, physical/human evidence or combinations thereof.

For important outcomes enough lineage must be retained to explain who/what acted, with which model/tool, inputs, permissions, policy version, result and validation.

## 17. Privacy, placement and budgets
Missions define data-placement policy: this machine only, private LAN, specific approved cloud/provider or prohibited external transfer classes. Routing must enforce this before context leaves a zone.

Budgets exist at portfolio/project/team/model/action level, including LOCAL_FREE, BYOK and cloud spend caps with warnings and automatic stop conditions. Cost optimization cannot silently violate a user's model/privacy policy.

## 18. Skills and extensibility
Repeatable processes are versioned skills containing instructions, tools, templates, validation, risk policy and acceptance criteria. Skills can be personal, project, organizational or imported.

Nika exposes a capability-oriented catalog: users ask what they want to do (mail, calendar, repository, accounting, research) and Nika resolves available connectors/skills and permissions.

Core architecture should support open tool/context/agent interoperability protocols where useful without binding the kernel to one vendor or protocol.

## 19. Computer use and remote control
Tool preference is: reliable API/structured connector -> deterministic automation -> accessibility/semantic UI automation -> visual automation. Critical actions must not rely solely on screen coordinates.

A remote client may inspect status, approve/reject a pending action, pause/resume, reprioritize, obtain artifacts or trigger emergency stop without automatically exposing workstation secrets.

Voice is an additional interface, never the only path to a critical function. Complete keyboard/screen-reader equivalence remains mandatory.

## 20. Final accessible user experience
Launching NikaCore.exe should open a semantic overview, for example: active projects, working agents, decisions required, critical failures and recently accepted results.

Default shortcut contract (user-remappable):
- Ctrl+N new mission;
- Ctrl+O open project;
- Ctrl+P quick search;
- Ctrl+Shift+P command palette;
- Ctrl+1 overview;
- Ctrl+2 projects;
- Ctrl+3 agents;
- Ctrl+4 human decisions;
- Ctrl+5 results/artifacts;
- Ctrl+6 models/resources;
- Ctrl+7 events/automations;
- Ctrl+8 audit/evidence;
- Ctrl+9 settings/permissions;
- Ctrl+Shift+Space global pause;
- Ctrl+Alt+Shift+Space emergency stop for external effects;
- F6/Shift+F6 next/previous region;
- Alt+Left/Right navigation history.

All controls, tables, trees, graphs, statuses and progress must expose semantic name/role/state/focus. Graphs have equivalent textual structure. Color is never the sole information carrier. NVDA live announcements are rate-limited so background changes do not constantly interrupt speech.

Simple mode exposes goals, progress, required decisions and finished results. Expert mode exposes agents, models, environments, permissions, dependencies, evidence, costs and routing.

The user manages goals/rules/priorities/decisions rather than hundreds of chats.

## 21. Market-leap benchmark contract
Nika may be designed to surpass current products, but 'better than X' is allowed only after equivalent scenario benchmarks.

Required benchmark scenarios include:
1. develop/test/package a Windows project and recover after interruption;
2. operate three independent software projects for 24 hours;
3. complete a cross-domain business mission spanning mail/docs/spreadsheets/calendar;
4. research a contested question with source lineage and adversarial review;
5. execute a fully local/private mission without external context transfer;
6. migrate an active mission between models without state loss;
7. recover deterministically from machine/agent/model failure;
8. operate all core functions keyboard-only with NVDA;
9. run a paper-trading strategy with immutable risk boundaries;
10. distribute one mission across multiple user-owned machines;
11. use an external agent as a least-privilege contractor;
12. reconstruct a complete decision/action/evidence audit trail.

Compare time-to-accepted-result, human interventions, rework, cost, recovery, evidence quality, accessibility, privacy, risk control and provider/model portability.

## 22. Relation to current delivery
This expansion MUST NOT create a new scheduler, queue, CodingWorker, verifier, QA stack, ModelGateway, persistence engine, desktop shell, installer or release framework where canonical components already exist.

Immediate priority remains shipping the current Windows product and completing the real Development Factory E2E. Product VNext capabilities are layered onto that foundation in dependency order.
