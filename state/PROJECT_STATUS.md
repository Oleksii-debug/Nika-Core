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

PF1 successor #101 (`auto/pf1-product-decisions`) is **RED / NOT INTEGRATED** on inspected head `1e0c234d16aed11d0f158f0f9c9b7f90b77bd833`: Core #694 and M12 #462 failed. Until a repaired exact candidate integrates, PF5 must continue to fail closed for durable product-decision writes and must not bypass PF1 with direct SQL.

### PF2 — orchestration
PF2 #92/#93/#94/#97/#98 are **INTEGRATED**. The public surface includes durable ProductProject identity/spec/row-version binding for coordinator checkpoints and fail-closed stale resume. PF5 consumes public snapshots only and does not duplicate PF2 persistence/recovery ownership.

### PF3 — execution/deployment/credentials
PF3 #95/#99 are **INTEGRATED**. Downstream-safe surface includes execution nodes/capabilities/resources/leases, exact release/deployment/health/rollback snapshots and opaque project-scoped credential/identity reference policy with bounded leases, revocation/rotation and audit-safe restart state.

PF5 does not handle raw secret material, issue credential handles, enumerate unrelated credentials, perform deployment-provider actions, or copy PF3 broker internals into presentation code.

### PF4 — acceptance gatekeeper
PF4 remains the independent PF0–PF12 acceptance/evidence lane. PF5 presents integrated state but does not award PF0–PF12 acceptance credit on PF4's behalf.

### PF5 — command journey/release owner
PF5 #90/#96 are **INTEGRATED**.

Current real PF5 code/evidence PR is #100, `auto-pf5/project-scoped-command-center`, based on exact current main `c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff`. Rollback snapshots remain preserved at `backup/auto-pf5-100-118e41d9` and `backup/auto-pf5-100-249a9ca4`.

PR #100 is now a large Command Center integrity/security batch, not the earlier narrow five-file draft. The same root-cause family is closed across all currently integrated PF1/PF2/PF3 presentation inputs:

- **single-project composition boundary:** one `ProductCommandCenter` combines durable PF1 detail with PF2 coordinator, PF3 execution/deployment and integrated PF3 Credential Broker snapshots;
- **single-version PF1 read:** visible ProductProject detail and internal opaque credential references come from one repository read, preventing a spec update from racing between two independent presentation reads;
- **PF2 identity/evidence integrity:** project identity, component identity and work identity must be unique and target-scoped; worker result `work_id/component_id/repository_id/base_sha/job_id` must match its request; review evidence without a result fails closed; accepted state requires an accepted independent review;
- **PF3 execution integrity:** duplicate node/lease identities, one node assigned to multiple active leases, unknown-node leases, empty lease identity and invalid lease lifetime fail closed before presentation; only target-project leases and their nodes are shown;
- **PF3 deployment integrity:** duplicate intent/staging/current-release identities fail closed; health evidence must match environment + exact release; rollback evidence must match environment + failed release; HEALTHY and ROLLED_BACK states require corresponding successful evidence; foreign-project deployment state is filtered;
- **PF3 credential presentation:** ProductProject-declared opaque credential refs are reconciled against project-scoped broker snapshot state; active, revoked, missing and broker-only/unlinked states are represented textually; missing/revoked declared credentials become explicit blockers;
- **credential confidentiality:** raw `secret_ref`, protected handle material and raw Credential Broker audit detail are never serialized into ProductProject presentation; stable visible IDs use one-way SHA-256 of the opaque reference; oversized audit identities are hashed rather than copied;
- **credential cross-project fail-closed:** duplicate secret/identity/audit identities, target identity bound to foreign secret, foreign identity bound to target secret, provider mismatch and cross-project audit evidence are rejected rather than silently filtered;
- **bounded accessible text:** credential labels/details/evidence are bounded to the existing ProductStatus contracts and credential audit evidence is capped to the latest 20 events per credential so a corrupt or pathological snapshot cannot create an unbounded Command Center payload;
- **semantic status identity:** duplicate `(ProductStatusKind, item_id)` remains a final fail-closed guard and blocker count is recomputed only after the full project-scoped composition.

The expanded regression matrix now attacks cross-project leakage, corrupt PF2 result/review binding, corrupt execution leases, corrupt deployment health/rollback state, credential redaction, revoked/missing blockers, identity/audit cross-binding, duplicate identities, oversized metadata and bounded audit evidence. No new dependency, migration, provider action, raw secret surface or shared UI edit was added.

Earlier #100 candidates obtained green/substantial evidence but are superseded whenever source or upstream `main` changes. Only the final exact PR head after this expanded batch may receive GREEN/merge credit.

## Release/provenance deep-research result

PF5 re-audited current M12 release mechanics against current official GitHub and SLSA guidance. Current M12 already binds the package manifest to exact source SHA and verifies package/UIA/recovery behavior, but the uploaded ZIP is not yet backed by a GitHub cryptographic artifact attestation. Official GitHub Artifact Attestations can bind a released binary/ZIP to repository, workflow and commit using Sigstore/OIDC; GitHub documents this as SLSA v1 Build Level 2 provenance for artifact attestations. Current official new-implementation action is `actions/attest@v4`. This is **researched/prepared, not yet implemented in #100**; it is the next PF5 release-integrity batch after #100 integration so Command Center safety and release workflow ownership are not mixed during review.

## Shared/manual ownership

Scheduled Product Factory workers do not edit active manual DEV01–DEV05/M10 production slices. Current relevant owners include DEV01 #86, DEV02 #72, DEV03 #67, DEV04 #78, DEV05 #89 and M10 #61/#62.

DEV04 #78 retains Interaction/UIA/shared semantic UI ownership. PF5 does not edit shared DesktopBackend/web/UIA files.

## Accessibility and UI truth

The primary user remains Windows/NVDA-first. Automated semantic/UIA/WebView2 tests never set `NVDA_VERIFIED=true`. PF5 currently advances API/textual Command Center contracts while shared UI ownership remains separate.

Interaction priority remains:
1. native/application API;
2. DOM/UIA/accessibility semantics;
3. named deterministic controls;
4. vision/OCR fallback;
5. coordinates last.

## Product Factory acceptance truth

Backend contracts are not Product Factory completion. PF11 still requires the representative request through the real factory: research, durable ProductProject, required product decision, acceptance criteria, dynamic team, repository, isolated implementation, independent QA/accessibility, package/release provenance, restart/resume and explicit human-only items.

The representative expense application is an acceptance scenario, not code hard-coded into Nika Core.

## Release/package truth

No Product Factory Windows candidate is promoted from PF5 #100. M12 ZIPs created while validating isolated PF5 backend/presentation candidates are CI evidence only. A later human candidate requires a fresh exact integrated `main` package after all required Product Journey behavior is integrated. Shared packaged WebView2/UIA remains mandatory and PF5 does not weaken it.

## Next dependency-ordered wave

1. Finish the expanded #100 code/status batch, obtain fresh exact-head Core + M12, recheck live `main`, and integrate only if there is zero incompatible drift.
2. PF1 owner repairs #101 and integrates a durable public product-decision lifecycle; only then may PF5 replace its fail-closed decision placeholder with the real public API.
3. After #100 integration, PF5 opens one large release-integrity batch adding official GitHub artifact attestation for the exact Windows ZIP plus deterministic verification/truth regressions; SBOM adoption remains separate unless the release dependency surface and license evidence justify it.
4. Shared semantic UI wiring waits for DEV04 ownership release plus an explicit compatibility decision.
5. PF11 packaging/release follows only after the representative integrated journey exists.

No invented Full Product Vision percentage is assigned. Progress is reported through exact executable acceptance states: IMPLEMENTED, GREEN, INTEGRATED, PACKAGED, HUMAN_TESTED and NVDA_VERIFIED.
