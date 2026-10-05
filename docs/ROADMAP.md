# Nika Core — roadmap and progress truth

Baseline created: 2026-08-17. Scope reconciled: 2026-08-19. Product-factory scope expanded: 2026-08-20.

Progress is acceptance-gate based, not commit-count based. Regressions may reduce readiness. **Historical Core milestone credit and the expanded Full Product Vision are separate measurements.** See `docs/FULL_PRODUCT_VISION_2026-08-19.md` and `docs/AUTONOMOUS_PRODUCT_FACTORY.md`.

## Track A — Core foundation milestone history

The original M0–M12 roadmap measured the reusable Nika Core control plane. Its historical evidence remains useful and is not erased by the expanded product scope.

| Stage | Historical weight | Goal | Historical evidence truth |
|---|---:|---|---|
| M0 Research/reuse/governance/bootstrap | 6% | architecture, adoption map, repo rules, CI | GREEN / INTEGRATED |
| M1 Kernel foundation | 10% | config, registries, SQLite migrations, task state, audit, workspace/plugin contract, keymap | GREEN / INTEGRATED |
| M2 Durable agent runtime | 11% | AgentRuntimePort, cancellation, retries, approvals, durable checkpoint/resume | GREEN / INTEGRATED |
| M3 Memory/scheduler/resources | 9% | memory namespaces, scheduler, resource budgets/fairness | GREEN / INTEGRATED |
| M4 Model Gateway/tools/MCP | 8% | mock/no-LLM provider, Ollama, cloud/OpenAI-compatible, tools/MCP | GREEN / INTEGRATED for that historical scope |
| M5 Accessible Windows GUI foundation | 11% | WebView2 shell, semantic UI, keyboard/focus/keymap | GREEN / INTEGRATED for foundation scope |
| M6 Agent Builder/permissions | 8% | validated agent configs, permission review/activation | GREEN / INTEGRATED |
| M7 Multi-agent laboratory foundation | 9% | teams, handoffs, bounded fan-out, evaluator | GREEN / INTEGRATED |
| M8 Experiment/self-learning engine foundation | 10% | versioned experiments, metrics, champion/challenger, rollback | GREEN / INTEGRATED |
| M9 Plugin/workspace + interaction adapter foundation | 8% | plugin contracts, Software Factory boundary, semantic adapters | GREEN / INTEGRATED for foundation scope |
| M10 Security/reliability foundation | 5% | restrictions, approvals, backup/recovery/threat hardening | GREEN / INTEGRATED for foundation scope |
| M11 Windows packaging/distribution foundation | 3% | standalone Windows package, manifest/checksums/licenses | GREEN / INTEGRATED foundation; candidate lineage can be invalidated by later product changes |
| M12 Pre-human full-system gate | 2% | automated release gate + human Windows/NVDA acceptance | historical automated gate existed; current human candidate status must follow live integrated product truth |

The old statement “98% final A–Z product progress” is now reclassified as **98% historical Core roadmap credit before Full Product Vision reconciliation**. It must not be presented as 98% completion of the final Nika product.

## Current Core readiness truth

The historical Core is real and substantial: durable runtime, memory/scheduler/resources, provider-neutral ModelGateway, tools/MCP, accessible Windows shell, Agent Builder, multi-agent execution, experiment engine, plugin/workspace boundaries, security/recovery and Windows packaging all exist and have prior green evidence.

However, a 2026-08-19 audit proved that accessible-looking task controls in the packaged Windows UI were not fully wired to the real backend. The repair is being handled as a separate large product-journey change and invalidates the old artifact as a final human candidate until the repaired integrated product passes the complete release gate.

Therefore:
- historical Core evidence remains recorded;
- current packaged-candidate readiness must be re-earned on the final integrated repair;
- HUMAN_TESTED remains false;
- NVDA_VERIFIED remains false;
- PRODUCTION_RELEASE_READY remains false.

## Track B — Full Product Vision roadmap

No numeric Full Product Vision percentage is assigned yet. First define and close the real capability gates; only then may weights be adopted. This prevents another misleading “almost finished” number when user-visible journeys or major workspaces are still absent.

### F1 — Real Windows command/task product journey
Goal: the packaged user can create/inspect/pause/resume/stop real tasks, see real agents/workspaces/state/errors, restart and continue where applicable, entirely through the accessible final window.

Current work: active Windows backend/UI repair and complete Product Journey gate.

### F2 — Multi-mode intelligence platform
Goal: one Nika system supports:
1. Deterministic Brain with **no model**;
2. Embedded Brain using Microsoft Foundry Local as the primary Windows implementation;
3. external local model servers such as Ollama;
4. cloud/API models.

Required evidence includes the deterministic planning/tool/approval gate, Foundry Local adapter contract tests, official Windows SDK dependency proof, then a real physical-Windows embedded-model inference proof. llama.cpp and ONNX Runtime GenAI remain measured fallback candidates rather than mandatory dependencies.

### F3 — Capability Escalation / Toolsmith + Software Factory
Goal: when a durable task lacks a capability, Nika can find/reuse/adapt/build/test/register a safe capability and resume the original task from checkpoint. The code path remains isolated and cannot silently rewrite production main or expand permissions.

### F4 — Universal Research Engine + Corpus/Knowledge
Goal: reusable source management, incremental crawling/change tracking, HTTP/API/browser/document extraction, deterministic filtering, optional model analysis, evidence/confidence, dedup, structured cards, review state, local corpus indexing and accessible reports. GrantScanner becomes one profile over this shared engine rather than a one-off crawler.

### F5 — Computer Interaction and Accessibility Repair as real product capabilities
Goal: semantic-first browser/Windows control, deterministic accessible explanations, robust fallback hierarchy, permission-aware actions and real packaged Product Journey proof. Automated UIA/Playwright evidence supplements but never replaces human NVDA testing.

### F6 — Model Engineering Lab and resource-aware local AI
Goal: benchmark embedded/Ollama/cloud/specialist models on versioned datasets; track quality/latency/RAM/CPU/GPU; manage model artifacts/licenses/checksums; support power/resource profiles and promote only measured winners.

### F7 — AI Trader real workspace
Goal: historical no-lookahead replay, odds/event snapshots, virtual bank, singles/combinations/portfolio exposure, time waves, versioned strategies, risk/drawdown metrics, held-out replay, live/prematch paper trading, deterministic/statistical learning plus optional model-assisted analysis, restart-safe sessions and accessible reports.

Financial autonomy uses explicit user-configured authorization profiles plus Nika risk/approval policy. Agents cannot silently widen permissions or budgets.

### F8 — Autonomous Product Factory
Goal: the user can describe a complete digital-product goal in natural language and Nika can manage the durable product lifecycle rather than only create one workspace or one coding task.

Required end-state capabilities include:
- durable `ProductProject` state spanning days/weeks/months;
- formal Research -> Product handoff;
- market/competitor/constraint research when requested;
- versioned requirements and acceptance criteria;
- dynamic specialist Team Composer rather than a fixed agent count;
- one-repository or multi-repository ProductRepositoryGraph;
- repository creation/connection under explicit policy;
- isolated implementation through replaceable CodingWorker adapters;
- independent QA/audit and accessibility review;
- build/package/release evidence;
- Deployment Fabric and approved staging/production promotion;
- multi-platform/remote execution nodes where the target cannot be built on the user's Windows machine;
- Credential/Identity Broker using opaque/scoped secret references;
- post-release operations, maintenance, regression and rollback.

The binding design and acceptance extension are `docs/AUTONOMOUS_PRODUCT_FACTORY.md` and `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md`.

Examples such as a messenger, social network, screen reader, browser-agent product or business application are **test classes/examples**, not products that must be hard-coded into Core. The strategic requirement is to build the reusable factory capable of managing such ProductProjects.

### F9 — Autonomous Business Factory
Goal: a user business objective can be researched, converted into opportunities/work orders and—after applicable policy/user gates—executed through Product Factory without hard-coding one niche or marketplace.

Required lifecycle:
`Business Goal -> Market Research -> Opportunity -> Lead/Channel -> Qualification -> Proposal -> Approval/Standing Policy -> Work Order/ProductProject -> Product Factory -> QA -> Delivery -> Payment/Invoice State -> Support`.

Business automation must obey platform rules, identity requirements, communication policy and progressive financial/contractual authorization. No spam, deceptive impersonation, prohibited account automation or self-expansion of money/account permissions.

Binding design: `docs/AUTONOMOUS_BUSINESS_FACTORY.md`.

### F10 — Real workspace/capability creation from the Nika command center
Goal: the user can describe a smaller new workspace/agent/capability in natural language; Nika routes it through Agent Builder/Software Factory/Toolsmith, creates it behind stable contracts, tests it, exposes it through the final accessible UI and preserves compatibility with existing workspaces.

This remains a lightweight path for capabilities that do not require a full ProductProject. Large product goals route to F8 instead.

Telegram is deliberately **not** an active planned workspace. If it is ever requested again, it is treated as a new optional workspace/ProductProject rather than a built-in roadmap requirement.

### F11 — Full Product integration/release
Goal: all capabilities claimed for a chosen release are integrated on one exact head, complete Core/Windows/security/accessibility/product-journey gates pass, a fresh candidate is built, and only that exact candidate enters the human Windows/NVDA protocol.

## Product Journey rule

Every user-facing capability must prove this complete chain:

`packaged Windows UI -> semantic action/command -> validated bridge/API -> real Nika service/runtime -> persisted state/result -> accessible visible feedback -> restart/resume where relevant`.

A subsystem with backend tests but no usable final-window path is not complete.

For Product Factory and Business Factory, backend coding tests are additionally insufficient: the product-level gates in `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md` must close.

## Reuse-first implementation map

Binding reuse sources now include:
- `docs/REUSE_CATALOG_2026-08-18.md` for the broad component inventory;
- `docs/INTELLIGENCE_REUSE_2026-08-19.md` for the newer Deterministic/Embedded Brain decisions;
- `docs/FULL_PRODUCT_VISION_2026-08-19.md` for end-state scope;
- `docs/AUTONOMOUS_PRODUCT_FACTORY.md` for durable product creation/operation scope;
- `docs/AUTONOMOUS_PRODUCT_FACTORY_ACCEPTANCE.md` for product-factory gates;
- `docs/AUTONOMOUS_BUSINESS_FACTORY.md` for business-orchestration scope.

Every lane uses **REUSE -> ADAPT -> CUSTOM (thin)**. Do not rebuild generic schedulers, planners, inference engines, browser engines, Windows automation stacks, coding workers, OCR/speech engines, vector databases, retry engines, resource monitors, build systems or deployment provider APIs when maintained compatible components satisfy the requirement.

## Parallel development

Source work is dependency-aware parallel-first. Independent research/adapters/tests may proceed on separate branches while integration follows actual dependencies. A blocked lane does not idle unrelated lanes. Shared contracts require an explicit compatibility decision.

Manual Deep Research developer chats may be real implementation lanes. When the user starts these manual Developer/Auditor pairs, scheduled workers should be paused or reassigned to complementary integration QA, release/package verification, regression hunting and cross-lane evidence rather than duplicating the same source ownership.

Product Factory work must additionally allocate explicit ownership by ProductProject/component/repository. Multiple automated tasks may cooperate, but they must not duplicate the same branch/component or compete to update shared canonical state.

## CI/release policy

Coherent PR/main gates run the shared verification harness on Ubuntu and Windows where applicable. Focused Windows/WebView2/package/security/embedded-model proofs are added when they provide material evidence. Do not rebuild the Windows EXE or download large models on every source push.

Base Nika Core remains model-independent and comparatively small. Models and heavyweight workers are optional components; updating the application must not require re-downloading unchanged local models or user data.

## Human truth

- HUMAN_TESTED: false until the designated person executes the exact manual protocol.
- NVDA_VERIFIED: false until the exact packaged candidate passes the real Windows/NVDA protocol.
- Automated semantic/UIA tests never award those states.
