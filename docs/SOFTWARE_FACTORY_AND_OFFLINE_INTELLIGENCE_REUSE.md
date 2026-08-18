# Software Factory and offline intelligence reuse audit

Updated: 2026-08-18.
Status: architecture/reuse decision only. No integration or milestone credit is claimed while M1/M2 executable CI is blocked.

## Software Factory objective
The user should be able to state an end product such as “build an accessible chess application” and let Nika coordinate planning, reuse research, implementation, testing, accessibility review and release work. Nika should not spend months recreating a coding-agent runtime that already exists.

## OpenHands — ADAPT as the first Software Factory coding-worker candidate
Official SDK: https://docs.openhands.dev/sdk
Official repository/license: https://github.com/OpenHands/OpenHands

Fresh upstream facts checked on 2026-08-18:
- OpenHands exposes a Python/REST Software Agent SDK rather than only a hosted chat UI;
- its SDK is intended for one-off coding tasks, maintenance and large multi-agent refactors/rewrites;
- ready-made tools include shell execution, file editing, browsing and MCP integration;
- an Agent Server can run agents behind a REST interface;
- core OpenHands and agent-server are MIT-licensed, while the `enterprise/` directory has separate licensing.

Nika decision:
- do not build a complete coding-agent shell/editor/tool runtime from scratch before a practical OpenHands proof;
- integrate only the MIT-licensed core/SDK/agent-server surface unless a separate licensed enterprise decision is made;
- Nika owns project state, roles, safety, branch policy, approval, acceptance gates and release truth;
- OpenHands is a replaceable implementation of a future `CodingWorkerPort`, not the owner of Nika projects;
- production modifications must happen in an isolated workspace/worktree/branch and return a patch/commit/evidence for review;
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
- product planner: final goal -> acceptance criteria and staged plan;
- reuse researcher: maintained libraries, licenses, current APIs and risks;
- architect: boundaries and migration/extension strategy;
- coding worker: OpenHands candidate or another provider behind `CodingWorkerPort`;
- test/reviewer: independent tests, regression and security review;
- accessibility reviewer: keyboard/NVDA/semantic requirements;
- release worker: package only tested exact SHA.

A single capable worker may perform several roles when that is more efficient. Multi-agent fan-out must be justified by measured benefit.

## Nika-owned Software Factory contracts
Do not leak OpenHands-specific objects into the domain. Future stable contracts should describe:
- repository/workspace identity and allowed paths;
- requested goal and acceptance criteria;
- branch/worktree isolation policy;
- allowed tools and network policy;
- patch/commit/result artifacts;
- test commands and machine-readable test outcomes;
- cancellation/deadline/resource limits;
- risk classification and approval requirements;
- provenance: worker implementation, model/provider, versions and source baseline.

## Offline/minimal intelligence objective
Nika must remain useful when no cloud API and no large local language model are available. This mode is explicitly **not** general conversational intelligence. It is deterministic/specialized autonomy for known tasks.

### Unified Planning — ADAPT as a formal planner candidate
Official docs: https://unified-planning.readthedocs.io/en/stable/
Official project: https://github.com/aiplan4eu/unified-planning
License: Apache-2.0.

Why useful:
- planner-independent problem modeling;
- automatic selection/invocation of compatible planning engines;
- plan generation and validation;
- classical/numeric/temporal planning support through available engines.

Nika decision:
- evaluate behind a future `DeterministicPlannerPort` for domains where actions, preconditions, effects and goals can be modeled explicitly;
- do not force natural-language tasks into formal planning when the domain model is unknown;
- plans remain subject to Nika permission and action validation.

Candidate proof:
- model a small deterministic workflow with explicit preconditions/effects;
- solve and validate a plan without an LLM;
- reject an impossible goal cleanly;
- re-plan after a simulated action failure/state change;
- prove the same Nika task contract can choose deterministic planner or LLM planner without changing task persistence.

### ONNX Runtime — REUSE for compact specialist neural models
Official Python API: https://onnxruntime.ai/docs/api/python/api_summary.html

Role:
- run local ONNX/ORT inference models behind specialist ports;
- suitable for classification, ranking, compact vision/audio or other task-specific inference where a trained model exists;
- keeps model execution separate from Nika orchestration.

Non-goal:
ONNX Runtime is an inference engine, not a reasoning agent. Installing it does not create a general “brain”. Every model must have a declared task, provenance, input/output contract, resource budget and evaluation evidence.

### Classical machine learning — REUSE only per measured task
Use maintained libraries such as scikit-learn for classification, clustering, ranking/regression or anomaly detection when a dataset and metric justify them. Do not add a large generic ML dependency to core merely to claim that Nika contains AI.

## Minimal intelligence stack
When no LLM is configured, Nika may combine:
1. deterministic state machines and workflow templates;
2. rules and validated action schemas;
3. full-text/search/ranking over approved local knowledge;
4. formal planning for domains with explicit state/action models;
5. specialist classifiers/regressors/ONNX models;
6. experiment metrics and bounded strategy selection.

This mode can route tasks, execute known procedures, classify information, compare candidates, plan in modeled domains and learn measured preferences. It must clearly refuse or escalate open-ended semantic reasoning beyond its evidence/capability.

## What we explicitly avoid building from scratch
- a full coding-agent terminal/editor/sandbox stack before OpenHands proof;
- a general Windows AgentOS before UFO² proof;
- an agent browser layer before Playwright/Browser Use proof;
- a custom formal-planning engine before Unified Planning proof;
- a custom neural inference runtime instead of ONNX Runtime;
- a giant mandatory “AI bundle” that makes core packaging depend on every optional model/provider.

## Packaging consequence
OpenHands, browser automation, vision models, transcription models and other heavyweight capabilities should be optional components/adapters. `Nika Core` remains a small control plane; models, sandboxes and specialist workers can be installed separately. This reduces download size, dependency conflicts and update risk.

## Next implementation rule
Do not implement these future adapters while M1/M2 remain unverified. When executable CI is restored, integrate M1 and M2 first. Then each candidate above receives a bounded proof branch and measurable adoption gate before becoming a dependency.