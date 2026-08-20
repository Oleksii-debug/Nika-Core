# PARALLEL EXECUTION BOARD — Nika Core

Updated: 2026-08-20.
Mode: **ACTIVE AUTONOMOUS PRODUCT FACTORY DEVELOPMENT — LARGE COHERENT BATCHES**.
Canonical technical evidence: live GitHub `main`, exact PR heads and current Actions. Drive owns automation routing/ownership/handoff truth.

## Evidence states
- PREPARED — scope/contracts/reuse decision ready.
- IMPLEMENTED — production-intended source/tests exist on a branch.
- GREEN — exact branch/PR head passed required automated checks.
- INTEGRATED — exact green candidate merged into `main`.
- PACKAGED — exact installable artifact built and checked.
- HUMAN_TESTED — a person completed the required manual protocol.
- NVDA_VERIFIED — the human NVDA protocol passed; automation may never award this state.

Current global truth:
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- `PF11=false`;
- historical Core percentages and stale Windows ZIPs are archival only.

## Canonical main

`c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff`

Integrated Product Factory foundation:
- PF5 #90 — command/presentation routing;
- PF1 #91 — durable ProductProject + Research→Product handoff;
- PF2 #92/#93/#94/#97 — team/repository graph, coordinator, CodingWorkerPort adapter and restart recovery;
- PF3 #95 — ExecutionNode + deployment/staging/health/rollback foundation;
- PF5 #96 — ProductProject create/inspect/update plus PF2/PF3 execution/deployment presentation, exact head `5beb29c70ee0e5e72f729ad555a49384b3308c9c`, Core #681 + M12 #449 green, integrated as `be7b4ab4abd59a3e373ad48562e023b07febc98c`;
- PF2 #98 — ProductProject-version-bound coordinator checkpoints, exact head `bba309b4713d385e6b7a653fffbdec313866d499`, Core #686 + M12 #454 green, integrated as `84998ea814fc5f99489ad583ab052b06cab2f12b`;
- PF3 #99 — opaque Credential/Identity Broker foundation, exact head `50a7083f6931e240b1b529073aba47138999188a`, Core #695 + M12 #463 green, integrated as `c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff`.

## Scheduled Product Factory dependency order

`PF1 → PF2 → PF3 → PF4 → PF5`

PF5 remains downstream-last and consumes only integrated upstream contracts. Work is organized as large root-cause families rather than micro-PR chains.

### AUTO-PF1 — ProductProject
PF1 #91 is **INTEGRATED**.

Inspected successor #101, `auto/pf1-product-decisions`, head `1e0c234d16aed11d0f158f0f9c9b7f90b77bd833`, is **RED / NOT INTEGRATED** because Core #694 and M12 #462 failed. Durable product-decision writes therefore remain unavailable to PF5. PF5 continues to fail closed and never writes PF1 decision state through direct SQL.

### AUTO-PF2 — orchestration
PF2 #92/#93/#94/#97/#98 are **INTEGRATED**. The integrated surface binds ProductProject `spec_version` + `row_version` to coordinator checkpoints and rejects stale restore.

PF5 #100 now validates the integrity of PF2 snapshot evidence before projecting it: project/component/work identity, result/request identity, result job ID and accepted independent-review evidence must be coherent. PF5 does not take over coordinator persistence/recovery.

### AUTO-PF3 — execution/deployment/credentials
PF3 #95/#99 are **INTEGRATED**. Public downstream-safe surface includes execution nodes/capabilities/resources/leases, exact release/deployment/health/rollback state, and opaque project-scoped credential/identity policy with bounded leases/revocation/rotation/restart evidence.

PF5 #100 consumes only these public snapshots. It validates internal snapshot identity/evidence relationships before presentation, scopes them to one ProductProject, and never handles raw secret material or protected handles.

### AUTO-PF4 — acceptance QA
PF4 remains the independent PF0–PF12 gatekeeper/evidence lane. PF5 cannot self-award PF acceptance from presentation state. Stale or mismatched exact-SHA evidence remains invalid.

### AUTO-PF5 — command journey + release
PF5 #90/#96 are **INTEGRATED**.

Current large code/evidence PR: #100, `auto-pf5/project-scoped-command-center`, based on exact integrated main `c0e5564b0ee20ada8a1a9c380aa8f8dfec4ff0ff`. Rollback history is preserved at `backup/auto-pf5-100-118e41d9` and `backup/auto-pf5-100-249a9ca4`.

#### #100 large coherent root-cause family

The earlier narrow project-filtering batch was deliberately expanded after the same trust-boundary defect was found in multiple integrated snapshots. The current batch now covers:

1. **Single durable PF1 read** — visible project detail and internal opaque credential refs are captured from one ProductProject repository read so presentation cannot mix two spec versions.
2. **PF2 scope + identity integrity** — exact project match, unique component IDs, unique work IDs, result/request identity equality, exact base SHA and CodingResult job ID binding; review without result fails; ACCEPTED requires accepted independent review evidence.
3. **PF3 execution integrity** — duplicate node/lease identities fail; unknown-node lease fails; one execution node cannot be assigned to multiple active leases; empty lease identity and non-positive lifetime fail; only target-project leases/nodes are presented.
4. **PF3 deployment integrity** — duplicate intent/staging/current-release keys fail; intent/environment/release project identity must agree; health evidence must match exact environment/release; rollback evidence must match exact failed release; HEALTHY and ROLLED_BACK require corresponding successful evidence; foreign-project deployment state is not shown.
5. **Credential Broker integration** — declared ProductProject credential refs are reconciled with the integrated PF3 Credential Broker snapshot; active/revoked/missing and broker-only/unlinked states are visible as safe textual status; missing or revoked declared credentials become blockers.
6. **Credential confidentiality** — raw `secret_ref`, protected handle material and raw audit detail do not enter ProductProject presentation; one-way SHA-256 IDs are used for stable visible credential identity; oversized audit IDs are replaced by hashes.
7. **Credential cross-project integrity** — duplicate secret/identity/audit IDs, target identity→foreign secret, foreign identity→target secret, provider mismatch and cross-project audit evidence fail closed.
8. **Bounded presentation** — credential label/detail/evidence lengths obey existing Pydantic contracts; audit evidence is capped to latest 20 events per credential to prevent pathological snapshot amplification.
9. **Final semantic identity guard** — duplicate `(ProductStatusKind, item_id)` fails closed after full composition; blocker count is recomputed only after all validated project-scoped entries are assembled.
10. **Adversarial regression family** — focused tests now cover normal target presentation plus corrupt result/review bindings, corrupt leases, corrupt deployment health/rollback, credential redaction, revoked/missing blockers, cross-binding, duplicates and oversized metadata.

No new dependency, migration, raw secret, provider action, shared DesktopBackend/WebView/UIA source or manual DEV01–DEV05/M10 production slice is changed.

State: **IMPLEMENTED / FINAL HEAD TO BE FROZEN FOR FULL EXACT-HEAD GATES / NOT INTEGRATED**.

#### Release-integrity deep research prepared behind #100

A current official-source audit was completed for the next large PF5 release block. Existing M12 already binds `release-manifest.json` to exact source SHA and checks per-file SHA-256, notices, broad tests and packaged UIA/focus, but the distributed ZIP itself is not cryptographically attested by GitHub.

Current GitHub Artifact Attestations use Sigstore/OIDC provenance and can bind an artifact to repository/workflow/commit; GitHub documents artifact attestation as SLSA v1 Build Level 2 provenance. The current action for new implementations is `actions/attest@v4`; verification is through `gh attestation verify`. This is **PREPARED research only** in #100. It will be implemented after #100 integration as one separate large release-integrity block with least-privilege job permissions and deterministic truth regressions. SBOM adoption is not being bundled casually into #100.

## Manual/shared ownership — no scheduled duplication

- DEV01 #86 — Research/Corpus report exports.
- DEV02 #72 — Windows worker containment proof.
- DEV03 #67 — deterministic trader replay/accounting/risk.
- DEV04 #78 — strict Windows UIA semantic vertical and shared Interaction/UIA ownership.
- DEV05 #89 — stable platform subtitle acquisition.
- M10 #61/#62 — authorization/approval security ownership.

PF5 does not edit these production slices without an explicit compatibility decision.

## PF5 interaction/UI rule

Shared semantic Windows UI remains outside PF5 ownership while DEV04 #78 owns that surface. PF5 advances API/textual Command Center contracts and release truth first.

Interaction priority:
1. native/application API;
2. DOM/UIA/accessibility semantics;
3. named deterministic controls;
4. screenshot/OCR/vision fallback;
5. coordinates last.

Automated semantic/UIA evidence never sets `NVDA_VERIFIED=true`.

## Collision and integration policy

1. One writer per production slice.
2. Separate branch per independent coherent lane.
3. Branch from latest compatible green main unless a real dependency requires otherwise.
4. Never import an unmerged sibling branch as canonical dependency.
5. Shared-contract edits require explicit compatibility decision and focused tests.
6. A blocked upstream lane does not idle independent downstream work.
7. Exact-head acceptance + final current-main compatibility are required before merge credit.
8. No direct scheduled-worker writes to `main`.
9. Do not churn CI with micro-formatting pushes; freeze a coherent head and repair the entire deterministic failure family if CI exposes one.
10. Superseded/stale package or CI evidence never transfers to a changed SHA.

## Product Factory release policy

Backend-only tests do not close Product Factory. PF11 requires a representative product created by the real factory from a clean packaged Windows Nika installation: research, durable ProductProject, product decision, acceptance criteria, dynamic team, repository, isolated implementation, independent QA/accessibility, package/release provenance and restart/resume.

The expense application remains an acceptance scenario, not a hard-coded Core product. M12 ZIPs produced while validating isolated PF5 backend branches are CI evidence only and must not be handed to the user as a fresh human candidate.

## Next dependency-ordered large wave

1. Freeze the expanded #100 head, run fresh exact-head Core + M12, inspect complete Windows/Ubuntu/package evidence, repair a whole root-cause family if red, then perform final live-main compatibility check and integrate only if safe.
2. PF1 owner repairs #101 and integrates a durable public ProductDecision lifecycle before PF5 replaces the fail-closed decision placeholder.
3. After #100 integration, PF5 opens one large release-integrity branch for GitHub Artifact Attestation of the exact Windows ZIP, least-privilege OIDC/attestations permissions, verification guidance and release-truth regressions.
4. Shared semantic UI wiring waits for DEV04 ownership release plus explicit compatibility decision.
5. PF11 representative integrated journey and a real fresh human candidate follow only after the required product-decision/UI/release surfaces are integrated.

Progress is evidence-based; no invented Full Product Vision percentage is assigned.
