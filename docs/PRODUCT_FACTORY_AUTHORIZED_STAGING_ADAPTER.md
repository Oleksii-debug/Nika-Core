# PF3 Authorized Staging Adapter

Status: PF3/PF6 implementation/evidence note. This does not authorize a production deployment.

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
- current-release snapshot state is the complete `ReleaseRef`; legacy SHA-only snapshot entries migrate
  only when exactly one backing exact release can be proven, otherwise restart fails closed;
- rollback exception, transport failure, malformed evidence, exact-identity mismatch or `succeeded=False`
  after an applied deployment remains `UNCERTAIN`; it is not converted into a terminal safe state.

### Rollback compatibility decision

The original public provider method `rollback(intent, previous_release_sha)` is retained so existing
provider implementations are not silently broken. A second optional capability,
`rollback_exact(intent, previous_release: ReleaseRef | None)`, carries the complete previous release
identity.

`DeploymentFabric` uses the exact capability whenever a previous release exists. If a provider implements
only the legacy SHA method and there is a previous release to restore, the fabric does **not** dispatch an
ambiguous SHA-only rollback: the deployment remains `UNCERTAIN` and later mutation stays blocked until
reconciliation. This prevents a same-SHA/different-version-or-artifact alias from being accepted as a
successful rollback.

`AuthorizedAnsibleStagingAdapter` implements `rollback_exact`. The runner receives
`nika_previous_release_version`, `nika_previous_release_sha` and `nika_previous_artifact_digest`. A
successful exact rollback must independently report all restored fields and they must equal the requested
previous `ReleaseRef`.

The legacy rollback method remains available for compatibility, but it cannot earn terminal rollback
credit for an existing previous release through `DeploymentFabric`.

### Process-crash durability boundary

The pre-dispatch marker above is written to `DeploymentFabric` state before the provider call and is
snapshot-visible before any provider result returns. This branch does **not** wire a synchronous SQLite
host-task checkpoint between that marker and the external provider effect. The integrated generic
`SQLiteStore` / `checkpoints` table is task-anchored, while current deployment execution contracts do not
carry the canonical host-task identity needed to reuse that authority safely.

Therefore this batch proves deterministic snapshot/restart idempotency once the fabric snapshot has been
durably captured, but it does **not** claim that an OS/process crash in the narrow interval between the
in-memory marker and an external provider effect is already persisted to disk. Closing that boundary
requires a compatibility-approved deployment checkpoint host using canonical task authority; it must not
be replaced with a second ad-hoc persistence authority or a fabricated task identity.

## Runner-side contract

The operator-controlled playbooks are not stored in ProductProject and are not generated by this adapter.
For a successful operation they must emit one normal Ansible `runner_on_ok` event from a task whose exact
name is `nika_pf3_result`, with `res.nika_pf3` containing only normalized text-keyed fields.

Required normalized fields:

- deploy: `applied: bool`
- health: `release_version: str`, `release_sha: str`, `artifact_digest: str`, `healthy: bool`,
  `observed_at: ISO-8601 aware datetime`
- exact rollback with a restored release: `succeeded: bool`, `restored_release_version: str`,
  `restored_release_sha: str`, `restored_artifact_digest: str`
- exact rollback with no previous release: `succeeded: bool` with no partial restored identity;
- legacy rollback compatibility path: `succeeded: bool`, `restored_release_sha: str | null`
- inspect when a release exists: `release_version: str`, `release_sha: str`, `artifact_digest: str`,
  `healthy: bool | null`
- inspect when no release exists: `release_sha: null`, no partial version/digest identity, and
  `healthy: bool | null`

`release_sha` is lowercase 40-character hex. `artifact_digest` is lowercase 64-character hex. Health,
inspect and exact rollback identities must equal the corresponding Nika-owned `ReleaseRef` values.

Operator-controlled health/inspect playbooks that still emit only `release_sha` must be upgraded before
they can be used with this adapter; missing version or artifact-digest evidence fails closed rather than
being inferred from the requested intent. Exact rollback playbooks likewise cannot omit one part of the
restored release identity.

Playbooks that handle credentials must use the backend's own protected credential mechanism and Ansible
`no_log` discipline. Raw credentials must never be returned in `nika_pf3`, passed in ProductProject, or
written to ordinary Nika logs/evidence.

## Production-promotion authority truth

This adapter is staging-only. It does not create or infer R4 authority. Until the canonical M10 trusted
approval authority is integrated on `main`, production promotion remains gated/deferred rather than being
implemented by provider configuration or a caller-supplied approval string.

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
- provider deploy exception and missing deploy evidence -> `UNCERTAIN`, snapshot/restart support, and
  same-intent idempotency without blind provider replay;
- applied deployment followed by health failure/mismatch -> `UNCERTAIN` with same-intent and restart
  idempotency, never blind deploy replay;
- unresolved same-environment effects blocking new mutation until reconciliation;
- unresolved staging invalidating staging authority and corrupt restart state with stale authority failing
  closed;
- rollback exception/failure/mismatch -> `UNCERTAIN` rather than false terminal success/rejection;
- SHA-only provider compatibility without issuing an ambiguous previous-release rollback;
- exact rollback binding previous/restored version + SHA + artifact digest;
- no-release inspection resolving uncertainty and permitting later fresh work;
- exact version/SHA/digest health binding, incomplete identity rejection and health transport failure;
- exact version/SHA/digest inspect normalization and uncertainty preservation;
- exact current-release snapshot identity plus unique-only legacy migration;
- same-SHA/different-artifact legacy snapshot ambiguity failing closed;
- duplicate normalized Runner result contracts failing closed;
- secret-shaped authorization reference rejection;
- playbook traversal and untrusted relative private-data path rejection;
- raw unrelated Runner output not escaping into evidence;
- deterministic normalized evidence identity.

No claim is made for a live remote-node, provider or production deployment in CI. No claim is made for
SQLite-backed process-crash persistence across the pre-dispatch external-effect boundary in this batch.
`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `PRODUCTION_RELEASE_READY=false`.
