# Nika Core Parallel-First Development Policy

Status: ACTIVE DEVELOPMENT POLICY

## Purpose

Nika Core must not be developed as a strictly serial roadmap where an unrelated later capability waits for an earlier milestone to be fully integrated. The project uses dependency-aware parallel development: independent work may be researched, designed, implemented, locally tested, and prepared simultaneously, while integration into the canonical branch remains ordered by real dependencies and acceptance gates.

This policy complements `docs/LARGE_BATCH_POLICY.md`, `docs/AUTONOMOUS_DEVELOPMENT_PROTOCOL.md`, `docs/ACCEPTANCE_GATES.md`, and `AGENTS.md`.

## Core rule

Every autonomous development cycle must inspect all roadmap areas and select the maximum set of genuinely independent, non-conflicting, high-value workstreams that can be advanced safely in that cycle.

Target operating range: 5-10 active workstreams when sufficient independent work exists. This is a concurrency target, not a requirement to manufacture shallow work.

Prefer 5 deep coherent workstreams over 10 trivial edits. Never split a single coherent subsystem merely to inflate concurrency.

A blocked workstream must not idle unrelated workstreams.

## Parallel workstreams

The standing workstream map is:

1. Kernel foundation: configuration, registries, persistence, migrations, audit, Action Registry, Keymap and durable contracts.
2. Durable agent runtime: runtime selection, execution loop, recovery, cancellation, approval boundaries and runtime adapters.
3. Memory, scheduling and resources: durable memory, queues, schedules, resource quotas and concurrency controls.
4. Model Gateway and tools: Ollama/cloud/no-LLM adapters, tool contracts, MCP integration and provider-neutral model access.
5. Accessible Windows interface: local web UI, pywebview/WebView2 shell, keyboard operation, focus/accessibility contracts and NVDA test scaffolding.
6. Agent Builder and permissions: structured agent specifications, natural-language-to-config flow, versioning, permissions and safety review.
7. Multi-agent laboratory: supervisors, subagents, teams, typed messages, delegation limits and bounded parallel execution.
8. Experiment and self-learning engine: strategies, replay/evaluation, champion/challenger, metrics, rollback and controlled adaptation.
9. Plugin/workspace SDK: stable extension contracts plus representative real workspaces without coupling them to the kernel.
10. Security, packaging and final QA foundations: sandbox interfaces, secrets/approval boundaries, backup/recovery, Windows packaging scaffolding and release-test infrastructure.

These workstreams are not permission to violate dependency boundaries. They are permission to prepare and implement independent layers against stable ports, contracts, mocks and test doubles before upstream integration is complete.

## Dependency graph, not serial roadmap

Roadmap milestones describe product maturity and acceptance, not an instruction to write code one milestone at a time.

Work may proceed in parallel in four states:

- PREPARED: research, design, contract or test harness exists.
- IMPLEMENTED: production-intended code exists and its own appropriate tests pass.
- INTEGRATED: code is combined with required upstream dependencies and passes the integration gate.
- PACKAGED / HUMAN_TESTED: distribution and human acceptance have actually occurred.

Never promote PREPARED or IMPLEMENTED work to INTEGRATED merely because code exists. `NVDA VERIFIED` remains human-only.

## Isolation rules

Parallel work must remain mergeable and comprehensible:

- Use isolated feature branches/worktrees or strictly separated modules when simultaneous edits would conflict.
- Establish stable ports/protocols/contracts first when downstream work depends on an unfinished implementation.
- Downstream code depends on Nika-owned interfaces, not directly on LangGraph, Microsoft Agent Framework, pywebview, WebView2, model-provider SDKs, schedulers or other external frameworks.
- Use mocks, fakes, deterministic adapters and contract tests to unblock downstream work safely.
- Avoid two workstreams editing the same central file unless coordination is explicit and sequential.
- Schema evolution must use migrations and preserve backward compatibility unless a documented breaking migration is approved.

## Large-batch rule inside every workstream

A workstream should deliver the largest safe coherent package possible, for example:

research -> reuse/adapt/custom decision -> architecture decision -> interfaces -> multiple production modules -> persistence/migration -> tests -> documentation -> status update.

Do not stop after one function, one test, one documentation edit or one small refactor when the surrounding coherent package can safely be completed in the same cycle.

## REUSE BEFORE REWRITE applies independently to every workstream

Before implementing infrastructure in a workstream:

1. Check current official documentation and maintained upstream solutions.
2. Classify the choice as REUSE, ADAPT or CUSTOM.
3. Prefer dependencies/adapters over copied third-party source.
4. Record version, license, rationale and tests.
5. CUSTOM requires a documented reason why maintained options do not satisfy the Nika contract.

Parallelism is not permission to create ten custom implementations of solved infrastructure.

## CI and blocker policy

A GitHub Actions, billing, hosted-runner, Windows-runner or external-service blocker in one workstream must not stop unrelated research, implementation, local/source tests or test-harness work in other workstreams.

However, blocked evidence remains blocked:

- do not mark a Windows gate green if the Windows runner never ran;
- do not merge dependency-sensitive work solely to bypass a blocked gate;
- do not weaken tests, permissions or acceptance criteria to increase throughput.

Expensive hosted CI remains reserved for coherent PR/main/milestone/Windows/WebView2/packaging/release gates. Parallel development should increase useful output, not burn hosted-runner minutes on every small commit.

## Integration rule

Development is parallel; integration is dependency-aware.

Before integration:

1. Confirm exact upstream SHA/contracts.
2. Rebase/retarget dependent work where required.
3. Run contract/unit/integration tests appropriate to the change.
4. Run platform-specific gates when the feature crosses a Windows/WebView2/package boundary.
5. Merge only when acceptance evidence supports the state being claimed.

A later milestone may contain extensive PREPARED or IMPLEMENTED work while an earlier milestone remains the current integration gate. This is expected and must be reported explicitly rather than hidden.

## Autonomous-cycle scheduling

At the start of every hourly autonomous cycle:

1. Read canonical GitHub status, open PRs, branches, blockers and acceptance gates.
2. Build/update the dependency graph of available work.
3. Identify all independent large coherent packages available now.
4. Normally select 5-10 active workstreams when there is enough safe work.
5. Allocate effort toward the highest-leverage packages rather than finishing one roadmap milestone serially.
6. If capacity is limited, reduce the number of lanes but deepen each selected package.
7. Never wait on one blocked workstream if another useful independent package can advance.
8. End the cycle by reconciling branch/SHA/status/test evidence for every touched workstream.

## Progress reporting

`state/PROJECT_STATUS.md` and GitHub Issue #1 must report a parallel workstream matrix for touched/current workstreams including:

- workstream / product capability;
- roadmap milestone(s);
- branch and exact SHA;
- PREPARED / IMPLEMENTED / INTEGRATED / PACKAGED / HUMAN_TESTED state;
- tests/evidence;
- current blocker, if any;
- next large coherent package.

Overall percentage remains acceptance-gate weighted. Parallel development may create substantial prepared/implemented work without immediately increasing the official integrated percentage.

## Throughput objective

The objective is a material acceleration in accepted engineering output by exploiting independent work. The system should target 5-10 concurrent workstreams when justified, but must not claim a literal 5x or 10x wall-clock speedup without measured evidence. Quality, recoverability, security, accessibility and truthful status reporting remain hard constraints.
