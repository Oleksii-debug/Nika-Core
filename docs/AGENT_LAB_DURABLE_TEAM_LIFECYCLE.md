# Agent Lab durable team lifecycle repair

Date: 2026-08-19.
Lane: AUTO03 generic Agent Lab / multi-agent lifecycle.
Current base main: `26b0e89118905395071a2e3ef9479e94e177817f`.
Original branch base: `f161a48b58cd7df8f247a5c99aadb8a6f34de712`.
Branch: `fix/agent-lab-durable-team-lifecycle`.
Backup of the pre-refresh candidate: `backup/auto03-durable-team-lifecycle-2356af26`.

## Main-line compatibility refresh

AUTO04 PR #52 changed only M12 workflow trigger semantics and the QA lineage regression. It is intentionally included in this candidate's base so exact-head acceptance uses the latest automatic upstream-trigger coverage. No AUTO03 product-source file overlaps PR #52.

## Problem family

The integrated M7 baseline persisted team/member lineage and exposed `recoverable_members()`, but supervisor execution still had three durability gaps:

1. a child was marked `RUNNING` before Nika persisted any pre-execution resume cursor;
2. restart discovery did not call the already-existing `AgentRuntimePort.resume`, so a persisted running child had no real recovery path;
3. result row, member state and result/error handoff were separate commits, allowing process loss or a write failure to leave contradictory evidence.

`TeamState` also had `completed` and `failed` values but no explicit safe finalization boundary.

## Reuse decision

**REUSE / ADAPT** the existing runtime contract and LangGraph durable cursor. `AgentRuntimePort.resume(RuntimeResumeRequest)` already exists, and the integrated LangGraph runtime already exposes `initial_resume_token(task_id, thread_id)` using its checkpoint thread identity.

**CUSTOM (thin)** Nika-owned transaction boundaries, restart routing, approval non-bypass and team-finalization policy. No new orchestration framework, dependency or SQLite migration is introduced.

## Execution transaction boundaries

Supervisor-created child identity and TASK handoff are written in the same SQLite transaction. The TASK payload therefore exists before any child can be considered restartable.

For a runtime advertising `DURABLE_RESUME`, fan-out obtains a non-empty runtime-provided initial resume token for every requested child before spawning any of them. If the runtime claims durable resume but provides no token factory or an empty token, fan-out fails before partial spawn.

Immediately before `runtime.run()` the store atomically commits:
- member state `running`;
- the initial resume token;
- audit evidence that execution started.

After runtime completion the store atomically commits:
- result row;
- RESULT or ERROR handoff;
- normalized member state and returned resume token;
- audit evidence that the child execution finished.

An injected handoff insert failure must roll back both the result insert and state transition. A late runtime result after `cancel_team()` must not overwrite cancellation.

## Restart routing

`MultiAgentSupervisor.recover_team(team_id)` operates only on non-root child members while the team remains active.

- `spawned`: runtime work has not crossed the durable start boundary, so Nika starts it with the persisted TASK handoff and first binds the initial resume cursor;
- `running`: Nika requires a persisted resume token and calls `runtime.resume(..., mode=CONTINUE)`;
- `waiting_approval`: Nika deliberately does nothing automatically. An approval decision is human-controlled state and must never be invented during crash recovery;
- terminal states are never replayed.

The exact activated Agent Builder definition is revalidated before recovery. A retired/non-active definition therefore cannot silently continue after restart.

## Team finalization policy

Team completion is explicit because a successful fan-out wave does not prove that nested or later fan-out is finished.

`finalize_team()` fails closed while any child is `spawned`, `running` or `waiting_approval`. When all children are terminal:
- one or more completed children => team `completed`, even if some siblings failed;
- failures with no completed child => team `failed`;
- only cancelled children => team `cancelled`;
- no child work => team `completed`.

This preserves worker-failure containment while preventing an automatically completed team from blocking legitimate subsequent/nested fan-out.

## Regression evidence

`tests/test_agent_lab_durable_team_lifecycle.py` covers:
- simulated process loss after durable start binding and restart through `runtime.resume`;
- persisted `spawned` child restart from TASK payload without a false resume;
- approval wait surviving restart without auto-resume;
- fail-before-spawn for a runtime that falsely advertises durable resume without an initial cursor;
- explicit mixed-success/failure finalization;
- all-failure finalization.

`tests/test_agent_lab_team_atomicity.py` covers:
- injected result-handoff write failure rolling back result/state;
- late runtime completion being unable to resurrect a cancelled child.

The existing M7 suite remains the compatibility baseline for quotas, activated-definition binding, privilege attenuation, bounded parallel fan-out, worker failure containment, cancellation propagation and evaluator aggregation.

## Evidence boundary

`IMPLEMENTED` means source/tests/docs exist on the branch. `GREEN` requires exact-head Core CI and release-gate evidence under the repository's repaired exact-candidate checkout lineage. `INTEGRATED` requires merge of that exact green head after a live-main compatibility check.

`HUMAN_TESTED=false`.
`NVDA_VERIFIED=false`.
