# Nika Autonomous Product Factory

Status: binding end-state architecture direction.
Updated: 2026-08-20.

## 1. Product-level objective

Nika is not intended to contain a prebuilt implementation for every possible domain. Its higher-level objective is to become a reusable autonomous digital product factory: the user can describe a business or product goal in natural language, Nika can research the problem and market, propose a product direction, form an appropriate specialist team, design the system, create and manage repositories, implement and test the product in isolated workspaces, build/package/deploy it, and maintain it over time within explicit user permissions, budgets, policies and acceptance criteria.

Examples such as a messenger, social network, screen reader, browser agent, business service or accessibility product are examples of product classes that the factory should eventually be able to manage. They are not requirements to prebuild those products into Nika Core.

The canonical principle is:

`build the factory, not every possible product`

## 2. ProductProject is a first-class durable entity

A long-lived product effort must not be represented as only one AgentTask or one CodingJob. Introduce a versioned durable `ProductProject` domain boundary.

A ProductProject must be able to persist at least:
- user goal and desired outcome;
- product hypothesis/problem statement;
- market/research evidence and decisions;
- requirements and acceptance criteria;
- architecture decisions;
- repositories and components;
- roadmap, milestones, epics and tasks;
- agent/team assignments and ownership;
- dependencies and blockers;
- budgets/resource envelopes;
- credentials/connectors by opaque reference only;
- risks, compliance and licensing decisions;
- builds, test evidence, releases and deployments;
- incidents, bugs, maintenance work and current operational state;
- artifacts and documentation;
- immutable decision/audit history.

A ProductProject survives application/PC restart and may run for days, weeks or months. Nika resumes from durable state rather than requiring the user to reconstruct project context from chat history.

## 3. Research-to-Product lifecycle

Universal Research and Software Factory must be connected by a formal product lifecycle rather than remaining independent subsystems.

Required high-level flow:

`Goal -> Research -> Problem/Opportunity Discovery -> Competitor/Constraint Analysis -> Product Options -> User Decision/Policy Gate -> Product Requirements -> Architecture -> Team Composition -> Implementation -> Independent QA/Audit -> Packaging -> Deployment -> Operations/Maintenance`

Research output must be able to become a versioned ProductProject input without manual copying. A ProductProject may also start directly from a user-supplied specification when research is unnecessary.

The user retains control over product-direction decisions that materially change scope, legal identity, spending authority or other declared approval boundaries.

## 4. Dynamic Team Composer

Nika must not assume a fixed number or fixed list of agents for all projects. A `TeamComposer` capability should derive an efficient team from the ProductProject, dependencies and risk profile.

Possible roles include product researcher, product manager/planner, requirements analyst, system architect, security architect, accessibility specialist, backend engineer, web engineer, Windows engineer, mobile engineer, data/ML engineer, DevOps/release engineer, QA/tester, independent reviewer/auditor and support/operations worker.

A small project may use one capable worker for several roles. A large project may use many independent workers. Role count is an optimization decision, not a quality metric.

When a new specialization is required during execution, the Agent Lab may create/activate a new role or request a missing capability through Toolsmith. Child agents never receive permissions exceeding the parent/project ceiling.

## 5. Multi-repository and multi-component product graph

Software Factory must evolve from a single-repository coding job into a product graph while preserving existing safe CodingWorker contracts.

A `ProductRepositoryGraph` may describe one or many repositories/components, for example:
- backend/API;
- web frontend;
- Windows desktop;
- Android;
- iOS/macOS;
- infrastructure/deployment;
- shared SDK/libraries;
- test/benchmark suites;
- documentation.

Every component has a stable identity, repository/ref ownership, build/test commands, dependency edges, release version and deployment target. Nika must prevent two workers from silently editing overlapping ownership without an explicit integration decision.

## 6. Product Factory execution pipeline

For an approved ProductProject, the factory must be capable of coordinating this pipeline:

1. turn the goal into explicit acceptance criteria;
2. research maintained reusable components and applicable standards;
3. choose REUSE / ADAPT / CUSTOM(thin) per subsystem;
4. create or connect the required repositories;
5. create isolated branches/worktrees/workspaces;
6. compose worker roles;
7. implement bounded coherent batches;
8. run deterministic tests, security checks, accessibility checks and domain-specific evaluations;
9. run an independent review/audit lane;
10. repair defects without weakening gates;
11. build/package release candidates;
12. deploy to an approved staging target where relevant;
13. run end-to-end acceptance and rollback checks;
14. promote to production only under the applicable policy/approval level;
15. maintain the product after release.

The factory may use OpenHands, Codex, other coding workers or future providers behind replaceable Nika-owned `CodingWorkerPort`/worker contracts. No one worker framework becomes the product architecture.

## 7. Deployment Fabric

A product is not complete merely because source code exists. Add a provider-neutral `DeploymentFabric` boundary for approved deployment targets.

Required capabilities should include, where applicable:
- deterministic build/package;
- staging deployment;
- database migration planning/execution;
- domain/DNS/hosting/cloud connector actions;
- health checks;
- deployment logs and provenance;
- rollback/previous-release restore;
- release promotion policy;
- environment separation (development/staging/production);
- bounded secret/credential use.

Deployment providers remain adapters. Nika owns deployment intent, policy, audit, state and release truth.

## 8. Remote Build and Execution Fabric

Nika must not assume the user's Windows PC can build or execute every product target. Add a versioned execution-node abstraction for approved workers such as:
- local Windows;
- isolated Windows worker;
- Linux CI/worker;
- macOS/Xcode builder when required for Apple targets;
- optional cloud/GPU worker;
- organization-managed on-prem worker.

Each node advertises platform/capabilities/resources, receives only scoped work, returns machine-readable build/test evidence, and cannot silently gain project secrets or broader network/filesystem access.

## 9. Credential and Identity Broker

Users may authorize Nika to work with GitHub, hosting, cloud services, APIs, freelancer/business platforms or other systems. Raw passwords/API keys must not be copied into prompts, model memory, Git, logs or project artifacts.

Introduce a `CredentialBroker`/`IdentityBroker` boundary:
- persistent secrets use OS/provider-backed protected storage where available;
- project state stores opaque credential references, not plaintext;
- workers receive short-lived or least-privilege scoped credentials when possible;
- model providers receive no secret material unless the connector contract explicitly requires it;
- every credential use is auditable;
- credentials can be revoked/rotated without rewriting project history;
- no worker may enumerate unrelated stored secrets.

## 10. Product Operations and Maintenance loop

A ProductProject remains active after v1 release. Add an operations lifecycle capable of:
- monitoring health and declared service-level indicators;
- ingesting crash/error reports and approved user feedback;
- tracking GitHub/provider issues and dependency/security advisories;
- opening maintenance tasks;
- creating isolated fixes;
- regression testing;
- versioned release/rollback;
- dependency upgrades;
- documentation updates;
- post-release accessibility regression checks.

No production self-modification is allowed outside the same isolated implementation, test, review and release gates used for ordinary development.

## 11. Business Factory

`Business Agent Lab` is not a single hard-coded freelancer bot. It is an optional orchestration layer that can turn a user business objective into research, opportunities and ProductProjects.

Canonical lifecycle:

`Business Goal -> Market Research -> Opportunity -> Lead/Channel -> Qualification -> Proposal -> User/Policy Decision -> Work Order/ProductProject -> Product Factory -> QA -> Delivery -> Payment/Invoice State -> Support`

Possible specialist roles include market researcher, opportunity monitor, lead qualifier, communication/sales agent, estimator, project manager, Product Factory coordinator, QA, delivery and support.

The exact business model is not hard-coded. It may be websites, software, digital services or another user-approved lawful model discovered through research.

External communication, account creation, contracting, publishing, financial actions and representation of the user must comply with platform rules and Nika authorization policy. No autonomous spam, impersonation or silent widening of financial/account permissions is acceptable.

## 12. Competitive product research and IP/compliance gate

Nika may research public competitor capabilities, public documentation, APIs, standards, user pain points and market behavior, then design an independent implementation with similar or improved functionality.

The Product Factory must distinguish legitimate functional/market research from copying proprietary source code, protected assets, credentials, trademarks or other restricted material. Add explicit license/IP/compliance evidence to ProductProject decisions and release gates.

Every adopted dependency/tool must record source/version/license and applicable distribution constraints. Generated products must keep third-party notices and provenance where required.

## 13. Acceptance proof for the autonomous factory

The factory is not considered complete because it can generate one source file. A representative acceptance scenario must start from a clean packaged Nika installation and a natural-language product request, for example:

`Create an accessible Windows personal-expense application. Research existing products first, propose the architecture, wait for the required product decision, create a repository, implement the product, test it, prepare the Windows package and documentation.`

The evidence chain must demonstrate, without manual code copying:
- research and product proposal;
- durable ProductProject creation;
- requirements/acceptance criteria;
- dynamic team/worker plan;
- repository creation or approved repository connection;
- isolated implementation branches/workspaces;
- independent tests/review;
- accessibility checks;
- successful package build;
- release artifacts/checksums/licenses;
- durable status and restart/resume;
- clear remaining human-only acceptance items.

A later deployment acceptance scenario must additionally prove approved staging deployment, health verification and rollback.

## 14. Relationship to workspaces

Universal Research, AI Trader, GrantScanner, Media, Accessibility Repair and other current workspaces remain useful products/capabilities, but they are not evidence that every possible future vertical must be manually prebuilt into Nika.

Generic capabilities belong in Core/Product Factory. Domain-specific products are created as versioned workspaces, plugins, adapters or independent external products depending on architecture. Telegram remains outside the active roadmap unless explicitly requested as a future ProductProject/workspace.

## 15. Product truth

The end-state objective is therefore larger than an agent laboratory and smaller than an impossible claim that one agent instantly creates any software. Nika must be able to manage arbitrarily long, bounded product-development programs using durable state, specialist workers, reusable tools, verification and human/policy gates. Project scale affects time, resources and required infrastructure, not the fundamental lifecycle model.
