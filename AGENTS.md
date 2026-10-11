# 2026-10-11 Closure-first recovery directive — binding worker selection

This is a narrow work-selection and convergence clarification. It does not bypass CI, licensing, security, release, owner/NVDA, or repository protection requirements. Existing accepted code/PR/evidence remains authoritative.

1. For the assigned plan, read the LIVE `MULTI_PLAN_CLOSURE_STATE.md`, not just migrated Drive lines. Identify first ACTIONABLE, NOT-DONE section and its canonical PR lineage; do not start another whole-section implementation or duplicate authority while a repairable canonical candidate exists.
2. Classify exact-head gates as **PASS / PROVEN_FAILURE / PENDING / SKIPPED / EXTERNAL** from completed run/job logs. On PROVEN_FAILURE, fix the *first causal code/test/lint issue* on the existing finisher, without downgrading checks. After each repair, refreeze a SHA, use new CI, and do not report PASS from a prior SHA.
3. In particular, Plan 1 §§1–2 have a converged child PR #1871. Resolve actual Core/M12 Ubuntu+Windows failures there first. Other plan stacks must similarly reuse their existing PRs rather than creating another nested duplicate. Recover dependent parents in topological order before any terminal integration.
4. Immediately after applicable exact-head gates pass: reconcile unique work with current main once, perform permitted integration, verify post-merge source/tests/status, then set **only the proved section** DONE in GitHub and its assigned Drive plan. A green isolated test, draft PR, or passing fixture alone is not DONE.
5. External physical Windows/NVDA/Web/Cloud product acceptance remains Plan 7. Do not let this final external scope block independently qualified intermediate components, and do not invent physical PASS from mocks. If any real failure remains, record precisely and continue a nonconflicting acceptance-critical task, not speculative polish.
6. Report each plan as `terminal_done/total`, first actionable section, exact PR/SHA, and a single independently evidenced blocker. No percentage without evidence. Never silently rewrite older DONE or create a second coordination authority.

This directive takes precedence over any suggestion in older docs to maximize PR count or perform optional hardening instead of closing the currently qualified section.

# Multi-Plan Parallel Closure Protocol v4 — owner directive 2026-10-08

This directive overrides conflicting global-sequential work-selection rules for the new project-plan architecture.

Before mutation read PROJECT_PLAN_INDEX.md, MULTI_PLAN_PARALLELISM_CONTRACT.md, MULTI_PLAN_CLOSURE_STATE.md, the assigned Drive plan and live GitHub.

- Plans 1–6 are independent engineering plans; there is no global earliest Section across plans.
- Within the assigned plan, audit/reuse existing implementation first, skip terminal DONE, and work the first ACTIONABLE unfinished Section.
- Existing Nika code/PR/evidence is not greenfield. REUSE -> REPAIR -> CONVERGE.
- Stable contracts/fixtures/mocks may unblock peer-plan dependencies, but they never count as whole-product/physical evidence.
- Plan 7 is final convergence/physical acceptance/go-live and is not an ordinary parallel lane before required Plan 1–6 outputs exist.
- Do not create duplicate persistence/runtime/security/scheduler/Capability Broker/ModelGateway/AgentRuntime authorities.
- Human Windows/NVDA and real Web/Cloud acceptance belong to Plan 7, not intermediate blockers.
- DONE is terminal unless demonstrated regression, invalid evidence, materially changed acceptance contract or breaking integration.

# Nika Core autonomous development instructions

## CANONICAL WORKER-AUTONOMY OVERRIDE

This section has highest priority for autonomous worker coordination and supersedes older conflicting worker-count, coordinator-count, WIP, ownership/claim, serialization, exclusive-integration, PR-order, and CI-wait restrictions anywhere in this repository.

- No repository-defined worker-count, coordinator-count, WIP, work-package, branch, or PR cap.
- Claims, leases, ownership, assignments, queues, and coordinator labels are advisory only and never block useful safe work.
- Workers may create branches, commits, PRs, fixes, tests, integration commits, and merges when GitHub permissions allow and evidence is adequate.
- Dependency order constrains final integration only; it does not stop independent implementation, testing, hardening, research, docs, accessibility, packaging, fixtures, adapters, or prototypes.
- Queued/pending/slow/unavailable CI is never by itself a reason to terminate. Record it and continue immediately with another valuable independent task.
- A blocked first workline is never by itself a reason to terminate. STATUS: BLOCKED is allowed only after all reasonably available safe independent work is exhausted.
- Do not idle because another PR, branch, worker, check, review, claim, or queue is active. If overlap occurs, switch to non-conflicting work or reconcile/rebase instead of abandoning the run.
- There is no repository-defined exclusive integration owner. Any authorized worker may integrate verified work when GitHub permissions and product/release gates allow.
- Use the full execution window while useful safe work remains.

This removes orchestration throttles only. Product correctness, security, data integrity, accessibility, licensing, privacy, truthful testing/release evidence, and domain-specific safety requirements remain mandatory.

This repository is the canonical source of truth. Chat history is not.

Current operating authority: **docs/AUTONOMOUS_WORKER_ORCHESTRATION.md**, policy DELIVERY-2026-09-09. The owner requires unrestricted parallel autonomous development with no repository-defined worker or coordinator cap. This supersedes older worker counts, blanket V0.1-only/Factory-first-only scheduling and global human-gate waiting freezes. It does not weaken product scope, security, accessibility or acceptance requirements.

Before each cycle read this file, the operating policy, the relevant live records in issue #553, and actual source/CI evidence for your assignment. On first adoption or relevant changes, read docs/MASTER_SPEC.md, docs/FULL_PRODUCT_VISION_2026-08-19.md, docs/ROADMAP.md and applicable Product Factory/Business Factory/Web/Cloud, reuse, UI and acceptance specifications. Reuse prior unchanged context; do not reread every historical document and branch on each hourly wake-up. coordination/AUTONOMOUS_ROUTING.md and state/PARALLEL_EXECUTION_BOARD.md are pointers to the same live control issue, not separate allocation authorities.

Primary rule: **REUSE BEFORE REWRITE**. Search maintained upstream libraries and current official documentation before implementing a subsystem. Default decision order is **REUSE -> ADAPT -> CUSTOM (thin)**. A CUSTOM decision is invalid unless it records why maintained upstream options do not satisfy the requirement. Do not copy random or wholesale third-party source into this repository when a package dependency/adapter is sufficient. Do not add broad unused dependencies merely “for later”; graduate a candidate through a focused proof, exact license/version check and tests.

Architecture: Windows-first modular monolith with ports/adapters and versioned contracts. Nika owns task/audit/permission/product contracts; provider-neutral Model Gateway; workspace/plugin boundaries; ProductProject and ProductRepositoryGraph contracts; Deterministic Brain contracts; deterministic state/validation/dedup/safety; and the Product Journey/Product Factory gates. Language models are replaceable capabilities, not the platform kernel. Agent orchestration sits behind `AgentRuntimePort`; third-party framework/model/planner/coding-worker types must not leak into Nika domain APIs.

Web/Cloud truth: Windows/NVDA remains the active first-class desktop release path, but the binding end-state also includes a real accessible Web/Cloud edition and optional Nika Node. New domain/application logic must remain presentation-neutral and should flow through stable command/query/application-service contracts so Windows and Web do not fork the Nika brain. Remote-desktop/video streaming is not the normal Web product architecture. Browser/local clients are untrusted for authentication, subscription/entitlement, ownership and permission truth; those decisions belong to server-side authority. Do not stop V0.1 to build the full Web product now—prevent new Windows-only domain coupling and preserve the later migration path defined in `docs/WEB_CLOUD_PRODUCT_ARCHITECTURE.md`.

Intelligence truth: Nika has four distinct paths that must not be conflated: (1) model-free Deterministic Brain, (2) embedded local model with Microsoft Foundry Local as primary Windows adapter and measured alternatives such as llama.cpp/ONNX Runtime GenAI, (3) external local model servers such as Ollama, and (4) allowed cloud/API providers. Installing or testing one does not award evidence for the others.

Runtime truth: M2 selected and integrated LangGraph behind `AgentRuntimePort`. Microsoft Agent Framework remains a secondary migration/interop candidate. Do not run multiple competing orchestration kernels in production unless a new measured proof demonstrates a concrete requirement.

Full-product truth: historical Core percentages do not equal completion of the expanded Full Product Vision. A backend subsystem is not finished until its actual packaged Windows user journey is connected and proven. Telegram is removed from active roadmap scope; old Telegram references in historical reuse documents are non-binding unless a future explicit user request reintroduces such a workspace/ProductProject.

Autonomous Product Factory truth: **build the factory, not every possible product**. Do not interpret examples such as a messenger, social network, screen-reader-like product, browser-agent platform or business application as requirements to hard-code those products into Core. The requirement is a durable ProductProject lifecycle that can research, specify, compose a specialist team, create/connect one or more repositories, implement in isolation, independently review/test, package, deploy under policy and maintain the resulting product. A large ProductProject may run for days/weeks/months and must not depend on chat memory.

Product-vs-Toolsmith truth: Toolsmith closes a narrow missing capability for an existing task. Product Factory owns complete product/system goals. Do not force a large product request into one oversized CodingJob or claim Product Factory completion from a coding-worker demo.

Dynamic-team truth: team roles are derived from ProductProject scope, dependencies and risk. One worker may cover several roles for a small project; a large project may need independent research, product, architecture, security, backend, frontend, Windows/mobile, QA, accessibility, DevOps/release and support roles. Agent count is not a success metric. New specialization may be added during execution without widening the ProductProject permission ceiling.

Research-to-Product truth: Universal Research is the canonical evidence/research layer for Product Factory and Business Factory. Where the user requests discovery/market/competitor analysis, research evidence and user/policy decisions must become versioned ProductProject inputs without manual copy/paste. Do not create a second research engine for Product Factory.

Repository/deployment truth: Product Factory may own one repository or a multi-repository ProductRepositoryGraph. Source generation is not product completion. Build/package, approved staging/deployment, health proof, rollback and maintenance are part of the product lifecycle where applicable. Use provider-neutral adapters and execution nodes; do not assume the local Windows laptop can build every platform target.

Credential truth: never put persistent passwords/API keys/OAuth material into prompts, model memory, Git, ProductProject state or ordinary logs. Product/workspace/business connectors use Credential/Identity Broker references and least-privilege scoped/short-lived credentials where supported. Workers cannot enumerate unrelated credentials.

Business Factory truth: Business Agent Lab is an optional reusable orchestration layer, not a hard-coded freelancer bot or one niche. It may research opportunities, qualify user-approved work, create WorkOrders/ProductProjects, coordinate delivery and track support/payment state. External communication, accounts, contracts, publishing and money movement obey platform rules and Nika authorization. No spam, deceptive impersonation, prohibited automation or self-expansion of financial/account authority.

IP/license truth: public competitor/product/market research may inform an independent implementation, but access to proprietary source/assets/credentials is not permission to copy them. Every adopted dependency/tool records version/license/provenance and relevant distribution obligations; missing/unacceptable provenance can block release.

Parallel-first rule: every cycle selects genuinely independent coherent packages that can be completed and integrated within the current WIP limit. There is no fixed active-package target or WIP cap; other developers advance assigned review, integration repair or disjoint portions of those packages. Dependencies constrain merge/integration order, not isolated research, contract design, adapter implementation, mocks, fixtures, tests or prototypes. A blocked lane must not idle unrelated lanes. Avoid fake parallelism and shared-file collisions; prefer clear lane ownership and stable ports. Product Factory work additionally requires ownership by ProductProject/component/repository. Use PREPARED / IMPLEMENTED / GREEN / INTEGRATED / PACKAGED / HUMAN_TESTED / NVDA_VERIFIED evidence states accurately.

Large-batch rule: do not stop after one file, one function, one lint error or one small PR if the same coherent subsystem can safely be carried through implementation, error/recovery behavior, tests, docs, CI and integration in the same cycle.

Manual Deep Research development: when the user creates manual Deep Research developer/auditor chats, treat developer chats as real coding lanes capable of large implementation batches, not research-only roles. Their paired auditors independently inspect live GitHub evidence. Scheduled workers should then be paused or reassigned to complementary non-colliding QA/release/integration/evidence work rather than duplicating the same source ownership.

Accessibility: blind primary user, Windows 11 + NVDA. Web-style desktop UI uses local semantic HTML inside pywebview/WebView2; keyboard-only operation, accessible names/roles, headings/landmarks, deterministic focus and text logs are mandatory. Packaged WebView2 UI Automation discovery is a specific gate. Automated accessibility tests do not equal human NVDA verification. Products created by Product Factory receive relevant accessibility review/gates rather than inheriting false credit from Nika Core. The future real Web client has a separate browser/screen-reader accessibility gate; Windows `NVDA_VERIFIED` must never be reused as Web accessibility evidence.

Hotkeys: every application command has a stable Action Registry ID and all application-specific shortcuts are user-remappable through the Keymap system. Do not scatter hard-coded shortcuts through UI code or break standard editing keys.

Safety: no secrets in repo; no token/session/browser profile files; dangerous send/delete/publish/financial/code-execution actions remain governed by preview/audit/approval. Runtime agents never self-modify production source directly or self-expand permissions. A Toolsmith/Software Factory capability proposal uses isolation + tests + compatibility/security gates before registration. Product Factory workers use the same isolation discipline and cannot promote failed candidates or deploy outside authorized scope. For persistent user credentials prefer OS-backed secret storage rather than plaintext configuration. Public-repository secret scanning is a permanent gate.

Git discipline: `main` must remain releasable. Use feature/fix branches and coherent commits. Independent lanes branch from the latest green main unless a real dependency requires otherwise. Never claim success without exact test evidence. Distinguish IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.

CI policy: coherent PR/main gates execute the shared verification harness on both Ubuntu and Windows. Focused Windows/WebView2/package/security/model-hardware/Product-Factory jobs may be added where they provide real evidence. Never weaken a check to obtain green. Stale runs for the same PR/ref may be canceled. Do not rebuild an EXE or download large models on every development push. Future Web/Cloud work adds separate API-contract, multi-tenant security, deployment and browser-accessibility gates without weakening Windows gates.

Persist useful source and ownership checkpoints. At the end of a cycle with new actionable evidence, update the worker-owned GitHub checkpoint in at most eight lines: practical outcome, role/run, PR/SHA, actual checks/state, blocker and next owner/step. Details stay on the canonical PR. Do not duplicate the full report into Drive or post unchanged hourly audits. User-facing reports explain practical capabilities first. Any authorized worker may promote reviewed and properly verified candidates to main when GitHub permissions and applicable product/release gates allow.
