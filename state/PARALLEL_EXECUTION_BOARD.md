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

Global truth: `PF11=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.

## Canonical main

`568b90f176a874d77bbc585501cc614daf1d246c`

Integrated dependency chain now includes PF1 #91; PF2 #92/#93/#94/#97/#98/#102; PF3 #95/#99; PF5 #90/#96. PF2 #102 adds canonical durable coordinator checkpoint hosting plus 1/5/25/100-component and 100-component/10-repository/10-wave restart qualification; exact head `75419f68b7531fe0c2c6fa46d5ce1c5e5ab95622`, Core #711 + M12 #479 green.

## Dependency order

`PF1 → PF2 → PF3 → PF4 → PF5`.

PF5 runs downstream-last and may consume only integrated upstream contracts. A main advance invalidates merge credit from an older PF5 exact head even when code paths do not overlap; compatibility must be refreshed and fresh exact-head gates rerun.

### PF1
PF1 #91 is integrated. The last inspected decision successor #101 was RED/not integrated on `1e0c234d16aed11d0f158f0f9c9b7f90b77bd833`, Core #694 + M12 #462. Known Core Ubuntu failure family was five Ruff `ISC004` findings. PF5 does not edit PF1 and continues to fail closed for decision writes until a repaired public lifecycle integrates.

### PF2
PF2 #92/#93/#94/#97/#98/#102 are integrated. PF2 now owns durable ProductProject-bound coordinator checkpoint/recovery and large-scale restart behavior in the canonical SQLite host. PF5 #100 validates PF2 snapshot identity/evidence before presentation but does not duplicate persistence/recovery ownership.

### PF3
PF3 #95/#99 are integrated. PF5 #100 consumes public execution/deployment/Credential Broker snapshots only after fail-closed identity/evidence checks. Raw credential material, protected handles and unrelated-project credentials remain outside PF5.

### PF4
PF4 #103 remains independent adversarial acceptance QA. It may reject upstream PF0–PF12 behavior even when PF5 presentation is safe. PF5 does not self-award PF acceptance.

### PF5 — current large batch
PR #100: `auto-pf5/project-scoped-command-center`.
Current base: `568b90f176a874d77bbc585501cc614daf1d246c`.

Large root-cause family in #100:
1. one durable PF1 read for visible detail + internal opaque credential refs;
2. PF2 exact project/component/work/result/job/review identity validation;
3. PF3 execution node/lease uniqueness, ownership and lifetime validation;
4. PF3 deployment intent/health/rollback exact environment/SHA validation;
5. project-scoped Credential Broker presentation for active/revoked/missing/broker-only credentials;
6. explicit blockers for missing/revoked declared credentials;
7. one-way credential presentation identities and no raw `secret_ref`/protected-handle/audit-detail disclosure;
8. fail-closed cross-project identity/secret/audit binding and provider mismatch;
9. bounded label/detail/evidence payload and latest-20 audit cap;
10. final semantic status identity uniqueness + blocker recount after full composition;
11. broad adversarial tests for every family above.

No new dependency, migration, provider action, secret storage, shared UI, release workflow or manual-lane source is part of #100.

Historical #100 lineage:
- `51254f0f...`: exact checkout passed, Core #718 stopped before pytest on exactly 3 Ruff findings (2×SIM102, 1×I001); repaired as one static-analysis family;
- `15afcc7b...`: Core Ubuntu became green and M12 Ubuntu advanced, but PF2 #102 moved main before merge credit; preserved at `backup/auto-pf5-100-15afcc7b`;
- refreshed branch now sits on integrated PF2 #102 main; old CI cannot transfer.

State: **IMPLEMENTED / CURRENT-MAIN REFRESH COMPLETED / FRESH FINAL CORE+M12 REQUIRED / NOT INTEGRATED**.

## Release-integrity successor — implementation-ready research

After #100 integration, PF5 will take one large release block instead of micro changes:
- calculate and persist the exact outer Windows ZIP SHA-256;
- generate GitHub Artifact Attestation for that exact ZIP;
- pin current inspected `actions/attest` v4.2.2 by full immutable SHA `1e69f48acb82d1966a394da916b4c1698aa569d6` rather than movable `@v4`;
- give only the packaged-release job `id-token: write` + `attestations: write` plus required read authority;
- verify attestation/repository/source identity and reject stale/superseded ZIP evidence;
- keep claims at SLSA v1 Build Level 2 unless a later reusable/hardened build workflow proves Level 3 requirements;
- do not bundle SBOM work merely for optics; treat it as a separate evidence decision if dependency/license requirements justify it.

## Ownership boundaries

Manual DEV01 #86, DEV02 #72, DEV03 #67, DEV04 #78, DEV05 #89 and M10 #61/#62 remain independent source owners. DEV04 retains shared semantic Windows UI/WebView2/UIA ownership. PF5 does not edit those surfaces without compatibility decision.

## Release / accessibility rules

Backend-only green does not equal PF11. Isolated PR M12 ZIPs are CI evidence only and never a human candidate. Final NVDA verification is human-only.

Interaction order remains native/application API → DOM/UIA/accessibility semantics → named deterministic controls → vision/OCR → coordinates last.

## Next large wave

1. Complete refreshed #100 exact-head Core + M12 and final main-drift check; merge only if both green and current-main compatible.
2. Let PF1 repair/integrate #101 independently; do not consume unmerged decision code.
3. Start one large PF5 ZIP-digest + Artifact-Attestation release-integrity branch immediately after #100 integration.
4. Only after required decision/UI/release dependencies integrate, assemble PF11 representative packaged journey and a genuinely fresh human/NVDA candidate.

No invented completion percentage is assigned.
