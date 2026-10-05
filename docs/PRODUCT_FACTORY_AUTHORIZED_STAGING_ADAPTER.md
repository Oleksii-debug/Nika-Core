# PF3 Authorized Staging Adapter

Status: PF3 implementation/evidence note. This does not authorize a production deployment.

## Decision

REUSE Ansible Runner rather than creating a Nika cloud API or SSH control plane. The adapter uses the
existing `DeploymentProviderPort` from `product_factory_deployment.py`; `DeploymentFabric` remains the
owner of staging-first, health, rollback and uncertain-reconciliation semantics.

Current upstream evidence checked on 2026-08-21:
- Ansible Runner documents a Python `run()` interface returning a Runner object with status, return code
  and event stream.
- Runner status includes successful/failed and callback-level canceled/timeout states, which is sufficient
  for Nika to classify mutating transport failures as uncertain rather than safe rejection.
- Ansible Runner 2.4.3 is the current release observed in the upstream release feed. It is an optional
  dependency only; base Nika installation does not pull it in.
- Ansible's official control-node documentation does not support native Windows as a control node. WSL is
  also explicitly not supported for production use. Therefore Nika's Windows desktop must not silently
  become an Ansible/SSH control plane: the real Runner dependency is installed only on non-Windows, and a
  real staging proof must execute on an explicitly authorized suitable execution/control node or service.

## Trust boundary

`AuthorizedAnsibleStagingAdapter` is deliberately narrower than a generic remote executor.

It accepts only an exact preconfigured tuple:
`project_id + environment_id + provider_ref + inventory + authorization_ref`.

Additional invariants:
- environment tier must be `STAGING`; production is rejected before Runner is called;
- playbook names are trusted leaf filenames from an absolute, operator-controlled Runner data directory;
- ProductProject/deployment intent cannot supply arbitrary shell, SSH argv, playbook paths or inventory;
- extravars contain only project/environment/intent identity, release version, source SHA, artifact digest
  and an opaque authorization reference;
- obvious raw secret-shaped values are rejected at the adapter boundary;
- raw Runner stdout and arbitrary Ansible events never become Nika evidence;
- exactly one explicit task named `nika_pf3_result` may provide the normalized `nika_pf3` mapping;
  duplicate result contracts and non-text contract keys fail closed;
- Nika evidence is a deterministic SHA-256 reference over normalized result identity, not raw output;
- failed/timed-out deployment is `uncertain=True`, because a remote mutation may have partially occurred;
- before calling the provider, `DeploymentFabric` places an evidence-less `UNCERTAIN` marker in its own
  state. A snapshot taken from inside the provider call therefore already contains the unresolved intent;
- a provider exception during `deploy()` or a deploy result with no evidence cannot be treated as a safe
  rejection: the marker remains `UNCERTAIN` without fabricated provider evidence, and an `UNCERTAIN`
  snapshot may therefore have no provider refs when the provider returned none;
- once a provider reports `applied=True`, health transport failure, malformed health evidence, or exact
  release mismatch remains `UNCERTAIN`; retrying the same intent returns that record instead of replaying
  the deployment mutation;
- any unresolved deployment effect blocks a different intent targeting the same project/environment until
  explicit `inspect()` reconciliation resolves the effect;
- an unresolved staging effect invalidates prior healthy staging authority for that project, so stale
  staging proof cannot authorize production while the staging environment is unknown;
- restart rejects a snapshot that tries to combine healthy staging authority with an unresolved staging
  effect for the same project;
- failed inspection raises and therefore preserves the existing uncertain deployment record rather than
  inventing a release state;
- health must prove the exact intended release version + source SHA + artifact digest and a timezone-aware
  observation time before the fabric may treat that `ReleaseRef` as healthy staging authority;
- inspect/reconcile must prove the same complete exact release identity whenever a release is reported;
  same-SHA/different-artifact or different-version evidence is rejected and uncertainty is preserved;
- current/staging public snapshot projections retain the integrated SHA-only tuple shapes for existing
  Product Command consumers, while additive exact-authority fields durably retain complete `ReleaseRef`
  version + source SHA + artifact digest identity;
- legacy snapshots without additive exact authority migrate only when durable records identify one exact
  release; ambiguous same-SHA histories fail closed;
- providers may retain the legacy SHA-only `rollback()` contract for compatibility, but when a previous
  exact release exists `DeploymentFabric` will not issue an ambiguous SHA-only rollback effect;
- providers implementing optional `rollback_exact()` receive the complete previous `ReleaseRef` and must
  prove the exact restored release; the Ansible adapter implements this capability;
- rollback transport failure, invalid/mismatched exact rollback evidence, or `succeeded=False` after an
  applied deployment remains `UNCERTAIN` and requires inspection.

### Process-crash durability boundary

The generic `DeploymentFabric` still establishes the pre-dispatch `UNCERTAIN` marker in memory before the
provider call. `DurableDeploymentFabric` closes the process-crash persistence gap by overriding only that
existing save boundary and synchronously passing the complete snapshot to
`ProductFactoryDeploymentCheckpointHost` before provider dispatch can continue.

The checkpoint host does not introduce a second database or deployment journal. It reuses the canonical
`SQLiteStore`, Product Factory host task identity and `CheckpointService`/`checkpoints` table. The host
requires the task payload to be explicitly typed as `kind=product_factory` and bound to the exact
`product_project_id`; a failed or mismatched checkpoint therefore prevents provider dispatch.

On restart the host selects the newest PF6 checkpoint by SQLite insertion order (`rowid DESC`) rather than
wall-clock metadata, verifies checksum plus canonical finite JSON, and fails closed on an invalid newest
row instead of silently falling back to older state. New checkpoint snapshots preserve both frozen public
SHA projections and additive exact release authority. Historical payloads without the additive fields
remain readable only through the existing fail-closed legacy disambiguation rules.

The crash-injection regression kills execution with `SystemExit` after provider dispatch begins, reopens
the same SQLite database through a new store instance and proves that replaying the same intent returns
the durable `UNCERTAIN` record without a second provider dispatch. Explicit provider `inspect()`
reconciliation remains the only route out of that uncertainty.

## Runner-side contract

The operator-controlled playbooks are not stored in ProductProject and are not generated by this adapter.
For a successful operation they must emit one normal Ansible `runner_on_ok` event from a task whose exact
name is `nika_pf3_result`, with `res.nika_pf3` containing only normalized text-keyed fields.

Required normalized fields:

- deploy: `applied: bool`
- health: `release_version: str`, `release_sha: str`, `artifact_digest: str`, `healthy: bool`,
  `observed_at: ISO-8601 aware datetime`
- legacy rollback: `succeeded: bool`, `restored_release_sha: str | null`
- exact rollback when a release is restored: `succeeded: bool`, `restored_release_version: str`,
  `restored_release_sha: str`, `restored_artifact_digest: str`
- exact rollback when no prior release exists: `succeeded: bool` with no partial restored-release identity
- inspect when a release exists: `release_version: str`, `release_sha: str`, `artifact_digest: str`,
  `healthy: bool | null`
- inspect when no release exists: `release_sha: null`, no partial version/digest identity, and
  `healthy: bool | null`

`release_sha` is lowercase 40-character hex. `artifact_digest` is lowercase 64-character hex. Health and
inspect identities must equal the exact `ReleaseRef` supplied by the deployment intent. Exact rollback
must equal the complete previous `ReleaseRef`; partial restored-release identity fails closed.

This tightens the original SHA-only adapter contract. Operator-controlled health/inspect playbooks that
still emit only `release_sha` must be upgraded before they can be used with this adapter; missing version
or artifact-digest evidence fails closed rather than being inferred from the requested intent.

The shared `DeploymentProviderPort.rollback()` remains available for legacy SHA-only providers. Exact
artifact rollback is exposed as the optional `ExactReleaseRollbackProviderPort.rollback_exact()`
capability instead of silently breaking the existing provider interface. When an exact previous release is
known but a provider lacks that capability, the fabric preserves `UNCERTAIN` and does not dispatch an
ambiguous SHA-only rollback. `AuthorizedAnsibleStagingAdapter` implements the exact capability and binds
previous/restored version + source SHA + artifact digest through its normalized contract.

Playbooks that handle credentials must use the backend's own protected credential mechanism and Ansible
`no_log` discipline. Raw credentials must never be returned in `nika_pf3`, passed in ProductProject, or
written to ordinary Nika logs/evidence.

## Real vs fake evidence

Production code includes a real optional `AnsibleRunnerClient` bridge for a supported non-Windows
control node. The focused tests inject a fake Runner execution port and a fake ansible-runner module
object; they do not connect to SSH, WinRM, a cloud provider, an inventory host, AWX/AAP or any external
deployment target. Windows CI exercises the provider-neutral adapter contract without installing or
pretending to run a native Windows Ansible control node.

This means the adapter contract and normalization boundary are implemented, while real staging mutation
remains unverified until an explicitly authorized staging environment, supported control node, inventory,
updated exact-release playbooks and protected credentials are supplied under the project's high-impact
authorization policy.

## Focused acceptance matrix

The test suite covers:
- exact staging target binding and no production use;
- project/environment/provider mismatch rejection before side effect;
- safe extravars with exact release version/SHA/digest and no password/token/secret fields;
- timeout/failure -> uncertain deployment;
- provider-visible pre-dispatch `UNCERTAIN` marker before a provider result returns;
- synchronous canonical SQLite checkpoint of pre-dispatch `UNCERTAIN` before provider mutation;
- process-death/restart from the same SQLite database with same-intent no-redispatch;
- insertion-order checkpoint authority under wall-clock rollback and newest-invalid-row fail-closed read;
- provider deploy exception and missing deploy evidence -> `UNCERTAIN`, snapshot/restart support, and
  same-intent idempotency without blind provider replay;
- applied deployment followed by health failure/mismatch -> `UNCERTAIN` with same-intent and restart
  idempotency, never blind deploy replay;
- unresolved same-environment effects blocking new mutation until reconciliation;
- unresolved staging invalidating staging authority and corrupt restart state with stale authority failing
  closed;
- failed rollback -> `UNCERTAIN` rather than false terminal success/rejection;
- exact `ReleaseRef` current/previous release persistence with frozen public projections and legacy
  snapshot migration;
- same-SHA legacy snapshot ambiguity failing closed;
- legacy SHA-only provider not being asked to restore an ambiguous exact prior artifact;
- exact-capable rollback binding previous and restored version/SHA/digest;
- partial or substituted restored exact release identity failing closed;
- no-release inspection resolving uncertainty and permitting later fresh work;
- exact version/SHA/digest health binding, incomplete identity rejection and health transport failure;
- exact version/SHA/digest inspect normalization and uncertainty preservation;
- same-SHA/different-artifact uncertain reconciliation rejection;
- duplicate normalized Runner result contracts failing closed;
- secret-shaped authorization reference rejection;
- playbook traversal and untrusted relative private-data path rejection;
- raw unrelated Runner output not escaping into evidence;
- deterministic normalized evidence identity.

No claim is made for a live remote-node, provider or production deployment in CI. The SQLite-backed
process-crash claim is limited to the task-anchored PF6 checkpoint boundary exercised by the synthetic
crash/restart tests; it is not evidence of a real remote deployment or cross-system transaction.
`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
