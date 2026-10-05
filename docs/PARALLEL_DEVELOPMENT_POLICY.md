# Nika Core Parallel-First Development Policy

Status: ACTIVE DEVELOPMENT POLICY

## Purpose

Nika Core uses dependency-aware parallel development rather than a rigid serial coding sequence. Independent work may be researched, designed, implemented and tested in parallel, while integration and release remain constrained by real dependencies and acceptance evidence.

This policy complements `docs/LARGE_BATCH_POLICY.md`, `docs/AUTONOMOUS_DEVELOPMENT_PROTOCOL.md`, `docs/ACCEPTANCE_GATES.md`, and `AGENTS.md`.

## Core rule

Each autonomous cycle inspects the full active roadmap and selects the maximum set of genuinely independent, non-conflicting, high-value workstreams that can advance safely. Prefer a smaller number of deep coherent packages over artificial concurrency. A blocked lane must not idle unrelated work.

Parallel work may be PREPARED or IMPLEMENTED ahead of an earlier integration gate, but it receives no weighted credit until its own exact acceptance evidence is GREEN and the change is INTEGRATED.

## Evidence states

Use these states literally:

- PREPARED — research, design, contracts, fixtures or test harness exist.
- IMPLEMENTED — production-intended code exists with appropriate source-level tests.
- GREEN — the exact candidate passed its required executable acceptance gate.
- INTEGRATED — the green candidate is merged into the canonical branch.
- PACKAGED — a release/package candidate was actually built and verified.
- HUMAN_TESTED — the designated human protocol was actually executed.
- NVDA_VERIFIED — the real Windows/NVDA human protocol passed; automation can never award this state.

## Isolation and dependency rules

- Use feature branches/worktrees or clearly separated modules for independent lanes.
- Establish stable Nika-owned ports/contracts before downstream implementation when an upstream implementation is unfinished.
- Domain code must not depend directly on LangGraph, pywebview, provider SDKs, automation frameworks, schedulers or other replaceable engines.
- Mocks, fakes, deterministic adapters and contract tests may unblock downstream work without pretending that integration already happened.
- Schema changes use explicit migrations and preserve fail-closed behavior for incompatible state.
- Avoid concurrent edits to the same central file unless coordination is explicit.

## Large coherent batches

Inside each lane, complete the largest safe coherent package available: reuse audit -> architecture decision -> contracts -> implementation -> persistence/migration -> tests -> error/recovery paths -> documentation -> evidence/status. Do not stop at one function, file or test when the related subsystem slice can safely be completed in the same cycle.

## REUSE BEFORE REWRITE

Every lane independently applies REUSE -> ADAPT -> CUSTOM (thin):

1. inspect current maintained upstream options and official documentation;
2. record version/license/rationale where a dependency graduates into the product;
3. prefer adapters over vendored source;
4. justify CUSTOM when maintained options do not satisfy the Nika-owned contract.

Parallelism is never justification for duplicating solved infrastructure.

## CI and blocker policy

A hosted-runner, billing, Windows-runner or external-service blocker in one lane must not stop unrelated useful source/test/research work. However, blocked evidence remains blocked: do not claim Windows GREEN if Windows never ran, do not merge merely to bypass a gate, and never weaken tests or safety policy for throughput.

Expensive Windows/WebView2/package/model proofs are reserved for coherent milestone and release gates rather than every development push.

## Integration rule

Development may be parallel; integration is dependency-aware. Before merge, confirm the exact upstream/candidate SHA, rebase or retarget if needed, run the required unit/contract/integration/platform gates, and merge only when the evidence supports the claimed state.

## Pre-human release freeze exception

Once an exact M12 pre-human packaged candidate has passed the automated full-system gate, normal feature expansion pauses. Parallel-first development does **not** justify changing production source while the only remaining weighted blocker is human Windows/NVDA acceptance, because any such change would invalidate the exact-candidate evidence.

During this freeze, safe autonomous work is limited to non-product maintenance that does not alter the tested candidate's behavior, such as canonical documentation/status consistency, evidence reconciliation, release-protocol clarification and investigation of concrete human-reported defects. If a human test finds a product defect, fix it on a new development candidate and rerun the complete automated M12 gate before further human acceptance.

## Current project application

At the current 98% state, M0–M11 and the M12 automated pre-human gate are already GREEN/INTEGRATED. The only weighted blocker is the human Windows/NVDA protocol bound to the exact M12 packaged candidate. Therefore the project is in the pre-human release freeze described above; no new feature lane receives credit or should mutate production source until human evidence or a concrete defect justifies a new candidate.

## Progress reporting

`state/PROJECT_STATUS.md` and GitHub Issue #1 remain the detailed canonical evidence surfaces. Report exact branch/SHA, evidence state, tests actually passed, blocker, unverified work and the next coherent batch. Overall percentage remains acceptance-gate weighted; prepared or implemented work alone does not move it.
