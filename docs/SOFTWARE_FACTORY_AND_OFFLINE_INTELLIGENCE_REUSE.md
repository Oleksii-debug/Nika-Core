# Software Factory and local intelligence reuse audit

Updated: 2026-08-19.
Status: binding reuse/adoption direction. Implementation credit still requires exact executable gates.

## Software Factory objective
The user should be able to state an end product such as “build an accessible chess application” and let Nika coordinate planning, reuse research, implementation, testing, accessibility review and release work. Nika should not recreate a complete coding-agent runtime that already exists.

## OpenHands — ADAPT as the first Software Factory coding-worker candidate
Official SDK: https://docs.openhands.dev/sdk
Official repository/license: https://github.com/OpenHands/OpenHands

Nika decision:
- do not build a complete coding-agent shell/editor/tool runtime from scratch before a practical OpenHands proof;
- integrate only compatible permissively licensed surfaces unless a separate licensing decision is made;
- Nika owns project state, roles, safety, branch policy, approval, acceptance gates and release truth;
- OpenHands is a replaceable implementation of `CodingWorkerPort`, not the owner of Nika projects;
- production modifications happen in an isolated workspace/worktree/branch and return patch/commit/test evidence;
- prefer official SDK/API/server integration; never automate the OpenHands web UI as a hidden workaround.

Required proof before adoption:
1. open an isolated test repository/worktree;
2. give a bounded implementation task with explicit allowed paths;
3. prohibit access outside the workspace;
4. run tests and return exact command/test evidence;
5. return diff/commit and artifacts without modifying Nika main directly;
6. cancel an active coding task;
7. recover or cleanly classify interrupted work;
8. verify no API keys, auth files, browser profiles or private logs enter commits;
9. compare glue-code volume against a thinner direct model + shell/file-tool adapter.

## Software Factory role model
Nika coordinates roles, not necessarily separate heavyweight agents for every task:
- product planner;
- reuse researcher;
- architect;
- coding worker;
- independent test/reviewer;
- accessibility reviewer;
- release worker.

A single capable worker may perform several roles when that is more efficient. Multi-agent fan-out must be justified by useful independence rather than agent count.

## Capability Escalation / Toolsmith
Software Factory is also the implementation arm of Nika's Toolsmith loop. A running agent that proves it lacks a capability may request a bounded capability addition. Nika first searches its existing Tool Registry/plugins and maintained upstream solutions; only then may a CodingWorker adapt/create the missing capability in isolation. The candidate must pass tests/security/compatibility gates, be registered/versioned normally, and allow the original task to resume from checkpoint. No direct production-main rewrite and no self-granted permission expansion are allowed.

## Nika-owned Software Factory contracts
Do not leak OpenHands-specific objects into the domain. Stable contracts describe:
- repository/workspace identity and allowed paths;
- requested goal and acceptance criteria;
- branch/worktree isolation policy;
- allowed tools and network policy;
- patch/commit/result artifacts;
- test commands and machine-readable test outcomes;
- cancellation/deadline/resource limits;
- risk classification and approval requirements;
- provenance: worker implementation, model/provider, versions and source baseline.

# Deterministic Brain — no LLM/model required

Nika must remain substantially useful when **no cloud API, no Ollama and no embedded language model exist**. This is not a mock chat provider. It is a first-class model-free agent capability for explicit/structured domains.

The stack combines:
1. deterministic state machines and workflow templates;
2. explicit facts/goals/actions/preconditions/effects;
3. formal automated planning;
4. the existing Nika Tool Registry/ToolExecutor;
5. full-text/search/ranking/dedup/provenance over approved knowledge;
6. classical statistics/ML for measured tasks;
7. durable memory/checkpoint/recovery;
8. experiment metrics and bounded strategy selection.

This can autonomously run many known procedures, search/filter/deduplicate, plan tool sequences, execute file/API workflows, produce reports, recover after interruption, operate replay/backtest loops and select measured strategies without a language model. It is not represented as open-ended GPT-level semantic reasoning.

## Unified Planning — ADAPT / IMPLEMENTATION CANDIDATE
Official docs: https://unified-planning.readthedocs.io/en/stable/
Official project: https://github.com/aiplan4eu/unified-planning
License: Apache-2.0.

Why useful:
- planner-independent problem modeling;
- automatic selection/invocation of compatible planning engines;
- plan generation and validation;
- classical/numeric/temporal planning support.

Nika decision:
- keep Nika-owned `WorldState`, goal, action and planner contracts;
- adapt Unified Planning behind those contracts;
- use a small compatible engine such as Pyperplan for the first Boolean-fact proof;
- do not force unknown open-ended natural-language tasks into formal planning;
- every planned external action still passes Nika ToolExecutor permissions/approval.

Required proof:
- plan and execute a useful multi-step tool workflow without ModelGateway;
- reject an impossible goal cleanly;
- re-plan after state change without repeating completed work;
- prove a high-impact planned tool remains blocked without normal approval.

# Embedded Brain — real local generative model without Ollama/API

## Microsoft Foundry Local — ADAPT as primary Windows embedded provider
Official repository: https://github.com/microsoft/Foundry-Local
Official Windows guidance: https://learn.microsoft.com/en-us/windows/ai/foundry-local/get-started

Fresh upstream decision, 2026-08-19:
- Microsoft provides an official Python SDK that can discover/download/cache/load/unload models and run chat inference through the local native core;
- embedded application scenarios can use the SDK directly in-process; a separate local web server is optional, not required;
- `foundry-local-sdk-winml` is the preferred Windows package and adds Windows ML hardware acceleration; do not install it alongside the mutually exclusive standard SDK in the same environment;
- model files and their licenses are separate from the SDK and must be audited per adopted model.

Nika integration rules:
- `FoundryLocalProvider` sits behind the existing ModelGateway `ModelProvider` contract;
- provider kind is LOCAL and sensitive/private requests may remain local;
- model download is disabled by default and requires explicit product permission so an agent cannot silently fetch gigabytes;
- installed models are optional components outside the base Windows package;
- Foundry SDK objects do not leak into Nika domain contracts;
- fake-SDK contract tests are not enough for final acceptance: a physical Windows hardware inference proof is required before production credit;
- the current official Python docs explicitly document cancellation for model/EP downloads, but not hard cancellation of an active inference call; Nika must record this limitation and must not claim a cancellation guarantee that upstream does not provide. Process isolation or another measured hard-cancel strategy may be added if the product gate requires it.

## llama.cpp — KEEP AS EMBEDDED FALLBACK CANDIDATE
Use when GGUF portability, CPU/Vulkan/other Windows execution paths, model availability or packaging benchmarks beat Foundry Local for a concrete Nika use case. Prefer a maintained binding/native adapter behind ModelGateway; do not vendor the entire project into Nika.

## ONNX Runtime GenAI — KEEP AS LOWER-LEVEL FALLBACK CANDIDATE
Use for direct generative ONNX inference only when its current API and Windows performance provide a measured reason to bypass Foundry Local's higher-level model management. It remains optional because its generative API is evolving.

## ONNX Runtime — REUSE for compact specialist neural models
Use for classification, ranking, compact vision/audio and other task-specific inference where a trained model exists. ONNX Runtime is an inference engine, not a general reasoning agent.

## Classical machine learning — REUSE only per measured task
Use maintained libraries such as scikit-learn for classification, clustering, ranking/regression or anomaly detection when a dataset and metric justify them. Do not add generic ML merely to claim that Nika contains AI.

## Model selection hierarchy
Nika's end-state intelligence choices are independent and replaceable:
1. Deterministic Brain — zero model;
2. Embedded Brain — Foundry Local primary, measured alternatives available;
3. external local server — Ollama/OpenAI-compatible local service;
4. cloud/API — allowed providers through ModelGateway.

Routing uses privacy, capability, latency/resource and user-policy constraints. No provider becomes the product architecture.

## Packaging consequence
Foundry model files, llama.cpp/other engines, OpenHands, browser automation, vision models, transcription models and other heavyweight capabilities stay optional components. Nika Core remains a small control plane. Program updates should not force re-downloading models or user data.

## Product Journey consequence
An intelligence adapter is not a finished user capability until it is reachable from the final packaged Windows product through a real task/agent path, produces accessible status/errors, preserves relevant state, and passes the Product Journey gate in `docs/ACCEPTANCE_GATES.md`.
