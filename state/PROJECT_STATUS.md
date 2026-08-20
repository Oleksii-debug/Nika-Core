# PROJECT STATUS — Nika Core

Updated: 2026-08-20.
Canonical repository: `Oleksii-debug/Nika-Core`.
Canonical technical truth: live GitHub `main`, exact PR heads and current Actions. Drive is routing/ownership/handoff truth.

## Practical product truth

Nika Core is in active Autonomous Product Factory development. Historical Core percentages and old Windows artifacts are archival evidence only; they do not prove the expanded Full Product Vision or PF0–PF12 acceptance.

Current human/release truth:
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- `PF11=false`;
- no stale ZIP may be promoted as a current Product Factory candidate.

## Canonical main

Current main at this reconciliation point:

`c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff`

Integrated Product Factory foundation includes:
- PF5 #90 — command/presentation routing foundation;
- PF1 #91 — durable ProductProject + Research→Product foundation;
- PF2 #92/#93/#94/#97 — team/repository graph, coordinator, CodingWorkerPort adapter and restart recovery;
- PF3 #95 — provider-neutral execution/deployment/health/rollback foundation;
- PF5 #96 — ProductProject create/inspect/update plus PF2/PF3 textual execution/deployment presentation, exact head `5beb29c70ee0e5e72f729ad555a49384b3308c9c`, Core #681 + M12 #449 green, integrated as `be7b4ab4abd59a3e373ad48562e023b07febc98c`;
- PF2 #98 — ProductProject-version-bound coordinator checkpoints, exact head `bba309b4713d385e6b7a653fffbdec313866d499`, Core #686 + M12 #454 green, integrated as `84998ea814fc5f99489ad583ab052b06cab2f12b`;
- PF3 #99 — opaque project-scoped Credential/Identity Broker foundation, exact head `50a7083f6931e240b1b529073aba47138999188a`, Core #695 + M12 #463 green, integrated as `c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff`.

## Product Factory dependency flow

### PF1 — durable ProductProject
PF1 #91 is **INTEGRATED**. PF5 consumes the public ProductProject create/get/update-spec and research-handoff contracts.

PF1 successor #101 (`auto/pf1-product-decisions`) is **RED / NOT INTEGRATED** on head `1e0c234d16aed11d0f158f0f9c9b7f90b77bd833`: Core #694 and M12 #462 failed. Until a repaired exact candidate integrates, PF5 must continue to fail closed for durable product-decision writes and must not bypass PF1 with direct SQL.

### PF2 — orchestration
PF2 #92/#93/#94/#97/#98 are **INTEGRATED**. The public surface includes durable ProductProject identity/spec/row-version binding for coordinator checkpoints and fail-closed stale resume. PF5 may consume it but does not duplicate PF2 persistence or recovery ownership.

### PF3 — execution/deployment/credentials
PF3 #95 and #99 are **INTEGRATED**. Downstream-safe surface now includes execution nodes/capabilities/resources/leases, exact release/deployment/health/rollback snapshots and opaque project-scoped credential/identity reference policy.

PF5 does not handle raw secrets, enumerate unrelated credentials, perform provider deployment actions, or copy PF3 credential-broker internals into Command Center code.

### PF4 — acceptance gatekeeper
PF4 remains the independent PF0–PF12 acceptance/evidence lane. It rejects stale/mismatched SHA evidence and must not become a competing feature writer.

### PF5 — command journey/release owner
PF5 #90 and #96 are **INTEGRATED**.

Current real PF5 code/evidence PR is #100, `auto-pf5/project-scoped-command-center`, rebuilt linearly from exact current main `c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff` after PF2 #98 and PF3 #99 integrations. Rollback snapshots are preserved at `backup/auto-pf5-100-118e41d9` and `backup/auto-pf5-100-249a9ca4`.

PR #100 closes one downstream presentation-integrity family:
- composes integrated PF1/PF2/PF3 presentation only through a project-scoped `ProductCommandCenter`;
- requires PF2 `CoordinatorSnapshot` identity and every work record to match the inspected ProductProject;
- filters global PF3 execution snapshots to target-project leases and only nodes serving those leases;
- filters PF3 deployment snapshots to target-project records/environment state;
- recomputes blocker count after composition;
- duplicate `(status kind, item_id)` presentation identity fails closed;
- existing PF3 provider/credential non-disclosure remains intact;
- focused tests attack foreign/corrupt coordinator state, cross-project execution/deployment leakage and duplicate presentation identity.

Earlier #100 candidates `118e41d9866e6e872ee3a91b5f827c0eb4fe4013` and `249a9ca4064ccccfd68b089ac1433a5e446710b5` obtained substantial exact-head green evidence but were superseded when PF2/PF3 advanced `main`; they receive no merge credit. Fresh exact-head Core + M12 are required for the final current-main rebuild.

## Shared/manual ownership

Scheduled Product Factory workers do not edit active manual DEV01–DEV05/M10 production slices. Current relevant owners include DEV01 #86, DEV02 #72, DEV03 #67, DEV04 #78, DEV05 #89 and M10 #61/#62.

DEV04 #78 retains Interaction/UIA/shared semantic UI ownership. PF5 does not edit shared DesktopBackend/web/UIA files.

## Accessibility and UI truth

The primary user remains Windows/NVDA-first. Automated semantic/UIA/WebView2 tests never set `NVDA_VERIFIED=true`. PF5 currently exposes native/API and textual presentation contracts only.

Interaction priority remains:
1. native/application API;
2. DOM/UIA/accessibility semantics;
3. named deterministic controls;
4. vision/OCR fallback;
5. coordinates last.

## Product Factory acceptance truth

Backend contracts are not Product Factory completion. PF11 still requires a representative request through the real factory: research, durable ProductProject, required product decision, acceptance criteria, dynamic team, repository, isolated implementation, independent QA/accessibility, package/release provenance, restart/resume and explicit human-only items.

The representative expense application is an acceptance scenario, not code hard-coded into Nika Core.

## Release/package truth

No Product Factory Windows candidate is promoted from PF5 #100. M12 packages produced while testing isolated PF5 backend candidates are CI evidence only, not human-bound Product Factory releases. Shared packaged WebView2/UIA remains mandatory and PF5 does not weaken it.

## Next dependency-ordered wave

1. PF5 proves the final current-main #100 rebuild with fresh exact-head Core + M12, then rechecks live-main compatibility before merge.
2. PF1 repairs #101 and integrates a durable public product-decision lifecycle before PF5 can claim create/inspect/update/decision completeness.
3. PF5 may consume integrated PF2 checkpoint identity and integrated PF3 credential-reference state in later presentation/journey work without duplicating owners.
4. Shared semantic UI wiring waits for DEV04 ownership release plus an explicit compatibility decision.
5. PF11 packaging/release follows only after the representative integrated journey exists.

No invented Full Product Vision percentage is assigned. Progress is reported through exact executable acceptance states: IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.
