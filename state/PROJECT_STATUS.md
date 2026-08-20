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

`568b90f176a874d77bbc585501cc614daf1d246c`

Integrated Product Factory foundation now includes:
- PF1 #91 — durable ProductProject + Research→Product foundation;
- PF2 #92/#93/#94/#97 — repository/team graph, coordinator, CodingWorker adapter and restart recovery;
- PF3 #95 — provider-neutral execution/deployment/health/rollback foundation;
- PF5 #90 — command/presentation routing foundation;
- PF5 #96 — ProductProject create/inspect/update plus PF2/PF3 textual presentation; exact head `5beb29c70ee0e5e72f729ad555a49384b3308c9c`, Core #681 + M12 #449 green;
- PF2 #98 — ProductProject-version-bound coordinator checkpoint contract; exact head `bba309b4713d385e6b7a653fffbdec313866d499`, Core #686 + M12 #454 green;
- PF3 #99 — opaque project-scoped Credential/Identity Broker foundation; exact head `50a7083f6931e240b1b529073aba47138999188a`, Core #695 + M12 #463 green;
- PF2 #102 — canonical durable coordinator checkpoint host + long-horizon scale/restart qualification; exact head `75419f68b7531fe0c2c6fa46d5ce1c5e5ab95622`, Core #711 + M12 #479 green, integrated as current main `568b90f176a874d77bbc585501cc614daf1d246c`.

PF2 #102 proves 1/5/25/100-component planning, 100-component/10-repository/10-wave checkpoint+restart progression, independent blocked-chain continuity, repair attempts, stale worker evidence rejection and checksum/stale-project fail-closed behavior through the canonical SQLite checkpoint host. PF5 consumes this only as integrated upstream durability evidence; it does not duplicate PF2 checkpoint storage.

## Current PF5 large batch — PR #100

Branch: `auto-pf5/project-scoped-command-center`.
Base: exact integrated main `568b90f176a874d77bbc585501cc614daf1d246c`.

PR #100 is one large Command Center integrity/security family, not a chain of small feature patches. It composes integrated PF1/PF2/PF3 public state only after validating project scope and evidence identity.

Implemented family:
- one ProductProject-scoped composition boundary for PF1 durable detail, PF2 coordinator snapshot, PF3 execution/deployment snapshot and PF3 Credential Broker snapshot;
- visible PF1 detail + internal opaque credential refs read atomically from one durable ProductProject version;
- PF2 validation for exact project, unique component/work identities, result↔request work/component/repository/base-SHA/job-ID binding, review/result coherence and ACCEPTED only with accepted independent review evidence;
- PF3 execution validation for duplicate node/lease identities, one-node/multiple-active-lease corruption, unknown nodes, empty lease identity and invalid lease lifetime; only target-project nodes/leases are exposed;
- PF3 deployment validation for duplicate intent/staging/current-release identities, project identity mismatch, health environment/SHA mismatch, rollback environment/failed-SHA mismatch, false HEALTHY and false ROLLED_BACK state; only target-project deployment state is exposed;
- PF3 Credential Broker presentation for active/revoked/missing/broker-only credentials without revealing raw opaque references or protected handles;
- declared missing/revoked credentials become explicit blockers;
- one-way SHA-256 visible credential identity; raw broker audit detail excluded; oversized audit IDs are represented by hashes;
- cross-project identity/secret/audit binding, provider mismatch and duplicate broker identities fail closed;
- presentation metadata is bounded to ProductStatus limits and credential audit evidence is capped to the latest 20 events per credential;
- final duplicate `(ProductStatusKind, item_id)` guard plus blocker recount after the complete validated composition.

Regression matrix covers normal and adversarial PF2/PF3/credential state, including corrupt result/review evidence, corrupt execution leases, corrupt deployment health/rollback, cross-project credential/identity/audit binding, missing/revoked credentials, duplicate identities and oversized metadata.

No new dependency, migration, raw secret, provider action, shared DesktopBackend/WebView/UIA edit, manual DEV01–DEV05/M10 source edit or release-workflow edit is part of #100.

### #100 CI lineage

A superseded head `51254f0fd1d3c20fede895509476db4d399ac48e` passed exact checkout identity but Core #718 stopped at exactly three Ruff findings before pytest: two `SIM102` and one `I001`. The whole static-analysis family was repaired without behavior weakening.

Repair head `15afcc7b039ac01da740145653f0c0bef8af05a3` subsequently reached Core Ubuntu success and M12 Ubuntu full source/recovery progress, but PF2 #102 advanced `main` before it could receive merge credit. Its history is preserved at `backup/auto-pf5-100-15afcc7b` and its CI cannot transfer to the refreshed candidate.

The current branch was rebuilt linearly from `568b90f...` after verifying PF2 #102 touched only its three PF2-owned files and had zero overlap with #100. Only fresh exact-head Core + M12 on the final refreshed #100 head count.

## Other dependency lanes

PF1 decision successor #101 was last independently inspected RED/not integrated on head `1e0c234d16aed11d0f158f0f9c9b7f90b77bd833`: Core #694 and M12 #462 failed. Its known Core Ubuntu failure family was five Ruff `ISC004` implicit-string-concatenation findings. PF5 does not edit that lane and continues to fail closed for durable decision writes until a repaired PF1 public API integrates.

PF4 #103 remains the independent PF0–PF12 adversarial gatekeeper. Its findings against upstream restore/release composition are not converted into false PF5 acceptance credit. #100 closes the downstream presentation-trust boundary; upstream PF1/PF2/PF3/release defects remain owned by their source lanes.

## Release-integrity deep research

The next PF5 block has been researched to implementation-ready depth against current official GitHub guidance:
- GitHub Artifact Attestations cryptographically bind a built artifact to repository/workflow/commit through Sigstore/OIDC and provide SLSA v1 Build Level 2 provenance;
- latest current `actions/attest` release inspected is v4.2.2, signed immutable release, exact commit `1e69f48acb82d1966a394da916b4c1698aa569d6`;
- GitHub secure-use guidance says a full-length commit SHA is the only immutable third-party action reference, so the next workflow batch will pin that exact SHA rather than use a movable `@v4` tag;
- OIDC/attestation write permissions will be job-scoped to the Windows packaged-release job, not granted workflow-wide;
- the exact final ZIP SHA-256 will be recorded and tested so release identity covers the outer distributable, not only files inside the release directory;
- attestation verification and stale/superseded-evidence rejection will be deterministic release-truth tests.

This attestation work is deliberately not mixed into #100; it is the next single large PF5 release-integrity batch after #100 integrates.

## Accessibility / release truth

Shared semantic Windows UI remains under DEV04 ownership. PF5 does not bypass it. Automated WebView2/UIA/keyboard evidence never sets `NVDA_VERIFIED=true`.

M12 ZIPs built for isolated development PRs are CI artifacts only. A real human candidate requires a fresh exact integrated-main package after the representative Product Journey and release gates are integrated.

## Next large wave

1. Freeze the refreshed #100 head on `568b90f...`, run full exact-head Core + M12 including Windows packaged proof, repair only a complete root-cause family if red, and integrate only after a final no-drift main check.
2. PF1 owner repairs/integrates the durable ProductDecision lifecycle before PF5 replaces its fail-closed decision placeholder.
3. After #100 integration, PF5 opens one large release-integrity batch: exact ZIP digest + full-SHA-pinned GitHub Artifact Attestation + least-privilege OIDC + verification/truth regressions.
4. Shared semantic UI wiring waits for DEV04 ownership release and an explicit compatibility decision.
5. PF11 representative journey and any human NVDA candidate follow only after those integrated dependencies exist.

No invented Full Product Vision percentage is assigned.
