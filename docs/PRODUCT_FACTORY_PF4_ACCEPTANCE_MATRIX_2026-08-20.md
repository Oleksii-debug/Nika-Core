# PF4 Product Factory deep acceptance matrix — reconciled 2026-08-21

## Canonical compatibility truth

PF4 is the independent Product Factory integration-gatekeeper and acceptance-QA lane. It owns executable acceptance tests, QA/integration fixtures and harnesses, exact-candidate lineage, compatibility evidence and independent rejection. It does not repair PF1/PF2/PF3/PF5 production behavior or manual DEV01–DEV05/M10 source.

Current canonical main is:

`e6025eb4b913d8fe4d550d94bd307a6b82644b99`

This main integrates PF1 compact shard-range commitments v2 from exact `7ab89f8d5ac625e23e8764996e5f9d3caae2a713` after Core #831 and full M12 #599 succeeded. The merge explicitly records the broader pre-existing archive/generation/chain-head/semantic/v1-shard numeric type-confusion surface as the next AUTO-PF1 follow-up. Repository metadata still reports `main.protected=false` and required-check enforcement off.

Current PF4 exact head is:

`445e049664c7ec6b7c5ca6b99079b6650aaf50fa`

Direct compare against current main is `behind=0`, merge-base exactly current main, and the diff contains only eight PF4-owned acceptance docs/tests. No production or release-workflow file is changed.

Acceptance policy:

- exact candidate-head Core + applicable M12 are authoritative;
- old or superseded SHA results are lineage evidence only;
- cancelled checks are neither GREEN nor functional RED;
- merge-ref success does not substitute for candidate-head evidence;
- production failures are never `xfail`ed, skipped or weakened to manufacture GREEN;
- PF4-owned false positives are corrected when independently established;
- automated UIA cannot set human or NVDA evidence;
- `HUMAN_TESTED=false`;
- `NVDA_VERIFIED=false`;
- `PRODUCTION_RELEASE_READY=false`;
- `PF11=false`.

## Quantitative acceptance history

The executable matrix has produced these meaningful states:

1. Initial deep matrix: **770 passed / 28 failed**.
2. Production integrations reduced the same historical matrix to **1018 passed / 17 failed**.
3. PF4 self-audit established two PF4 false-positive assertions and corrected only those tests.
4. Ownership-corrected baseline on PF1-integrated main: **15 failed / 1031 passed** on exact `8b2a8324...`.
5. New strict PF12 history-typing attacks on exact current head `445e049...`: **20 failed / 1031 passed** on both Core #846 Ubuntu and M12 #614 Ubuntu after successful exact checkout, dependency consistency, Ruff and compile.

The increase from 15 to 20 is **not a regression caused by PF1 #125**. It is five newly added adversarial tests against a follow-up surface explicitly named by that merge. The previous fifteen blocker families continue to reproduce unchanged.

Original-28 historical accounting remains:

- **11 historical product defects now pass their original adversarial assertions**;
- **2 PF4 assertions were false positives** and were corrected at the actual public-contract boundary;
- **15 previously known product/product-journey blockers remain**;
- **5 additional PF1/PF12 strict-numeric-typing defects are newly discovered**;
- current valid total: **20 blockers**.

## PF12 legacy-history numeric type confusion — 5 new defects

PF4 added `tests/test_product_factory_acceptance_pf12_history_types.py`. Every attack follows the same stronger construction:

1. build a real ProductProject on SQLite;
2. export the real public durable descriptor/archive;
3. replace one JSON integer identity with JSON `true`;
4. recompute the canonical SHA-256 envelope honestly;
5. call the public verifier and require `ProductProjectError`.

This bypasses superficial digest-tamper rejection and tests the semantic type validator itself. Exact Core #846 and M12 #614 Ubuntu show all five attacks failing with `DID NOT RAISE ProductProjectError`.

### PF12-T1 — archive `current_spec_version`

`ProductProjectHistoryArchiveService.verify()` accepts a rehashed archive whose `history.project.current_spec_version` is JSON `true`.

Root-cause family: legacy archive parsing coerces numeric fields through `int(...)`; in Python `int(True) == 1`.

Owner: PF1/PF12 ProductProject history.

### PF12-T2 — generation manifest `generation`

`ProductProjectHistoryGenerationService.verify()` accepts a rehashed manifest whose `generation` is JSON `true`.

Root-cause family: `isinstance(value, int)` accepts `bool` because `bool` subclasses `int`.

Owner: PF1/PF12 ProductProject history.

### PF12-T3 — chain-head descriptor `generation`

`ProductProjectHistoryChainHeadService.verify_descriptor()` accepts a rehashed chain-head descriptor with boolean generation identity.

Owner: PF1/PF12 ProductProject history.

### PF12-T4 — semantic-continuity descriptor `generation`

`ProductProjectHistorySemanticContinuityService.verify_descriptor()` accepts a rehashed semantic-continuity descriptor with boolean generation identity.

Owner: PF1/PF12 ProductProject history.

### PF12-T5 — v1 shard-index `generation`

`ProductProjectHistoryShardedCommitmentService.verify_index()` accepts a rehashed v1 shard-index descriptor with boolean generation identity.

Owner: PF1/PF12 ProductProject history.

### Required owner repair boundary

PF4 does not implement the fix. PF1 should use exact-integer semantics (`type(value) is int`, or one shared strict integer helper that explicitly excludes `bool`) for serialized numeric identities across the five legacy surfaces, preserve all existing digest/ancestry/range semantics and retain the already strict v2 range behavior. After owner-controlled repair: fresh exact-head Core + complete M12, integration, then unchanged PF4 rerun.

## Previously known valid blockers — 15

### PF2 coordinator semantic restore / PF12 — 4

1. forged `ACCEPTED` work without worker result or accepted independent review is accepted;
2. nested request repository identity drift is accepted;
3. nested request ProductProject identity drift is accepted;
4. restored allowed-path or permission expansion is accepted.

Owner: PF2 coordinator/recovery.

### PF2 implementation-evidence completeness / PF4 — 2

1. one passing command can stand in for multiple declared `acceptance_commands`;
2. an unrelated passing command can substitute for the declared acceptance matrix.

Owner: PF2 program-host / coding-worker adapter / coordinator reconciliation.

### PF2/PF3 team and repository integrity — 5

1. the same physical provider/locator repository may hide behind multiple logical repository IDs;
2. an integration decision may omit the actual conflicting active lease;
3. a dynamic specialist may own a phantom component absent from the project component set;
4. an empty opaque repository credential identity such as `credref:` is accepted;
5. an empty ProductComponent identity is accepted.

Owner: PF2/PF3 repository/team orchestration.

### PF7 credential audit monotonicity — 1

`CredentialBroker.restore()` accepts an audit-event counter rolled back behind already persisted event identities, permitting future durable identity reuse.

Owner: PF7 credential broker.

### PF10 license/notices verification — 1

A deliberately names-only `THIRD_PARTY_NOTICES.txt` passes `verify_third_party_notices()` without meaningful license evidence.

Owner: PF5/release-compliance packaging.

### PF11 packaged Product Factory composition — 1

`scripts/nika_windows.py` still does not compose the durable ProductProject/Product Factory command route required by the representative packaged factory journey.

Owner: PF5 packaged Product Factory composition. Shared UI/UIA remains separately owned.

### PF11 final distributable identity — 1

Current-main M12 creates the outer `NikaCore-<version>-windows-x64.zip` but does not record SHA-256 of that exact final uploaded ZIP in pre-human evidence. Internal release-manifest hashes do not identify the outer distributable.

PF4 owns the rejection test, not the repair. Drive routing assigns exact-SHA release integration / representative PF11 assembly / release plumbing to PF5.

## Historical defects independently proven repaired — 11

Current main passes the same original attacks for:

1. PF6 current-release state scoped by ProductProject + environment;
2. PF6 rollback isolation across ProductProjects;
3. PF7 token-shaped durable scalar rejection;
4. PF7 raw-token-as-credential-reference rejection;
5. PF1 phantom evidence-package rejection;
6. PF1 duplicate requirement identity rejection;
7. execution restore rejection of two active leases on one node;
8. execution restore rejection of invalid lease time ordering;
9. PF6 empty environment/provider rejection;
10. PF6 exact previous-release rollback restoration;
11. PF6 coherent restored current-release snapshot requirement.

These are integration credits, not full PF-gate closure.

## PF4 self-audit corrections retained

### Corrected stale ProductProject recovery boundary

The old test incorrectly expected one already-constructed immutable `ProductProjectCoordinatorBinding` to discover an out-of-band repository mutation. The correct recovery path re-reads current ProductProject state, constructs a new binding, and restores the old checkpoint. Current integrated behavior correctly rejects the stale checkpoint at that boundary.

### Corrected credential restart boundary

The old fixture reused the same in-memory fake protected-store object across a supposed process restart. The corrected test creates a new `WindowsCredentialStore` instance over the same OS-like backend plus a new broker. Current integrated behavior invalidates the old process-local lease/handle while preserving legitimate protected material for explicit re-issuance.

### Release-workflow ownership correction

A transient PF4 head patched `.github/workflows/m12-prehuman-release-gate.yml` to make the final-ZIP digest regression pass. This crossed the PF5 release-plumbing boundary. PF4 restored the workflow byte-for-byte to main and retained the strengthened regression. The transient 14-failure count is not accepted as gate truth.

## PF0–PF12 current classification

| Gate | PF4 classification | Current executable truth |
| --- | --- | --- |
| PF0 ProductProject | PARTIAL | durable/restart foundation; corrected stale rebind passes |
| PF1 Research→Product | PARTIAL/BLOCKED | provenance attacks pass; five legacy history strict-typing attacks fail |
| PF2 Team/orchestration | PARTIAL/BLOCKED | scale passes; semantic restore/evidence/phantom-component defects remain |
| PF3 Repository graph | PARTIAL/BLOCKED | multi-repo scheduling works; alias/conflict/identity attacks fail |
| PF4 Implementation | BLOCKED | review/restart works; acceptance-command completeness fails |
| PF5 Execution/Command Center | PARTIAL/BLOCKED | provider-neutral node contract works; packaged composition/release truth blocked |
| PF6 Deployment | PARTIAL/STRONG | historical scoping/rollback/restore attacks now pass; real-provider journey separate |
| PF7 Credentials | PARTIAL/BLOCKED | raw-secret/restart-handle attacks pass; audit monotonicity fails |
| PF8 Operations | NOT PROVEN | reusable foundations; no complete maintenance→repair→release/rollback journey |
| PF9 Business Factory | PARTIAL FOUNDATION | controlled durable experiment, not full factory |
| PF10 IP/license | BLOCKED | names-only notices verifier false-green remains |
| PF11 Representative packaged E2E | BLOCKED | composition + final ZIP identity + governance/human/NVDA remain |
| PF12 Long horizon | RED/PARTIAL | coordinator forged-restore family plus five legacy-history type-confusion defects |

## C0–C5 complexity qualification

### C0 — PASS foundation

Deterministic single component, stable work identity and one ready request.

### C1 — PASS foundation

Eight-component dependency/review chain with deterministic release after accepted independent evidence.

### C2 — scheduling foundation PASS / gate BLOCKED

Twelve components across four logical repositories schedule deterministically. Physical-repository alias rejection still fails.

### C3 — provider-neutral node contract PASS / real-provider acceptance absent

Windows/Linux use one execution-node contract with exact platform selection; unavailable macOS fails closed. This is not real remote-provider proof.

### C4 — scale foundation PASS

- 100-component dependency chain;
- ten restart boundaries;
- 100-component team fan-out;
- 50 non-overlapping ownership leases;
- 99-way progress around one blocker;
- integrated PF3 60-service / 180-replica fleet and rolling-maintenance evidence.

Real-provider selected verticals remain separate acceptance work.

### C5 — RED / PARTIAL

Passing foundation includes review persistence, superseded-attempt rejection and corrected stale-current-ProductProject rebind. Current blockers include forged coordinator restore/identity/permission drift and the five rehashed boolean historical-state type-confusion attacks.

## Live cross-lane gatekeeper notes

### PF1

PF1 #125 exact `7ab89f8d...` completed Core #831 + full M12 #599 GREEN and is integrated into current `main=e6025eb4...`. The five new PF12 findings target the explicit legacy follow-up left by that merge; they do not invalidate the exact acceptance evidence for the already integrated v2 range batch.

### PF2 #127

Recorded head `31fdda3bc7a8bece22100c3150b5eba1002b6ae3` was built on previous `449ed6dc...` and is stale after PF1 integration. Its exact M12 #605 was cancelled. PF4 has handed back a compatibility-refresh requirement; no current-main merge credit transfers.

### PF5 #128 / #129

#128 is stale on previous main and previously had Core GREEN with M12 failure isolated to packaged WebView2 UIA descendant discovery after both integrated OS jobs were GREEN. That evidence is shared packaged UIA/release proof, not a PF5 feature-regression reason.

#129 has a current-main refresh lineage but remains a simultaneous PF5 successor overlapping #128 in six Product Command paths. PF5 must explicitly converge/supersede the collision before integration; CI finishing first is not ownership resolution.

## Exact-head evidence

Current PF4 head `445e049664c7ec6b7c5ca6b99079b6650aaf50fa`:

- Core #846 Ubuntu: **20 failed / 1031 passed**, after successful exact checkout, dependency consistency, Ruff and compile;
- M12 #614 Ubuntu: **20 failed / 1031 passed**, same failure family after successful exact checkout, dependency consistency, Ruff and compile;
- Core Windows and M12 Windows were still executing at the latest evidence read;
- overall Core/M12 are already authoritative RED because required Ubuntu verification failed.

## Non-claims

This matrix does not claim real cloud/SSH deployment, real remote-machine execution, real payment/DNS mutation, arbitrary external credential-provider acceptance, complete PF8/PF9/PF10/PF11, human accessibility completion, NVDA verification or production release readiness.
