# Packaged ProductProject refinement journey

Status: ONE-SHOT-11 implementation evidence. This document does not grant PF11, HUMAN_TESTED, or NVDA_VERIFIED credit.

## Scope

After the integrated `Current/Show current ProductProject` journey, the next packaged step is a versioned refinement of the currently selected durable ProductProject. The application keeps the existing opaque project identity and records a changed goal as a new canonical ProductProject specification version.

User-facing path:

`goal -> create/select ProductProject -> show current identity/spec/state -> explicit goal refinement -> canonical ProductProjectCommandService update -> durable spec version -> textual result/focus -> restart -> recover same project and refined goal`

Supported explicit keyboard grammars are:

- `Set current ProductProject goal: <new goal>`
- `Update current ProductProject goal: <new goal>`
- `Встанови ціль поточного ProductProject: <нова ціль>`
- `Онови ціль поточного ProductProject: <нова ціль>`

The colon is part of the command grammar. Ordinary task text is delegated unchanged to the existing packaged ProductProject/AgentTask router.

## Authority and persistence

The refinement adapter is not a second ProductProject repository. `ProductProjectCommandService.update_project()` remains the only mutation authority used by this surface. It relies on canonical `ProductProjectRepository` optimistic concurrency and immutable specification history.

The packaged selection store remains presentation-only. It supplies the opaque current project ID after restart but is never treated as ProductProject authority. The selected ID is re-read through the canonical ProductProject service before a mutation.

A changed goal increments the current spec version exactly once. Repeating the same refinement is a no-op at this packaged boundary and does not create an empty duplicate version. The project ID is not recomputed from the new goal.

## Fail-closed behavior

- no selected ProductProject: reject and ask the user to create/open one;
- selected project no longer exists: clear only the stale presentation selection and reject;
- optimistic version conflict: reject with retry-from-fresh-state semantics;
- missing/empty/over-4000-character refinement: reject before mutation;
- token-shaped raw credential material: canonical ProductProject validation rejects it before a new spec version is stored;
- ordinary commands: delegate unchanged;
- no worker, Toolsmith, deployment, credential, approval, filesystem, shell, browser, UIA, vision/OCR, or coordinate authority is added.

A concurrent ProductProject lifecycle transition is not required to preserve the old presentation state after a successful spec update; the post-update service read is authoritative. This avoids falsely reporting failure after a legitimate committed mutation.

## Packaged evidence

`scripts/nika_windows.py --pf11-proof` now exercises the refinement through the real `UIActionBridge` `task.create` action. The first process creates spec v1 and refines it to v2. The second process uses the same SQLite database, restores the same ProductProject selection at v2, replays the same refinement as a no-op, and verifies the exact current project/spec/state/goal text and `tasks-heading` focus.

`scripts/m11_release.py` requires both process outputs to be byte-identical before emitting packaged journey evidence. It additionally requires spec version 2, non-empty refined goal, current-command/focus proof, refinement-command proof, durable refinement state, bounded presentation state, and false human/release claims.

## Ownership boundaries

This slice intentionally does not persist or own DynamicTeam lifecycle state (#163 family), trusted-plan/checkpoint authority (#164 family), C1 worker/repository/package acceptance (#319), shared DEV04 semantic UI/UIA source, or release attestation implementation (#142). Those remain independent lanes and are consumed only after integration through their public contracts.

## Evidence truth

Automated tests and packaged Windows evidence may prove the service/bridge/restart path, but they cannot set:

- `HUMAN_TESTED=true`;
- `NVDA_VERIFIED=true`;
- `PRODUCTION_RELEASE_READY=true`.

Those states remain false until their independent gates are actually satisfied.
