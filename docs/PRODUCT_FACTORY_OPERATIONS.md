# PF3 Product Operations and Maintenance Lifecycle

This batch adds provider-neutral product operations above the integrated PF3 execution-node, deterministic deployment, credential-broker, and Windows protected-store foundations. It does not add a cloud control plane, SSH implementation, hosting account integration, or production deployment path.

`DeployableService` binds one independently operable service to ProductProject identity, environment identity, an exact 40-character source SHA, staged rollout wave, replicas/nodes, minimum healthy-replica quorum, earlier-wave dependencies, and opaque credential references.

`ProductOperationsCoordinator.ready_services()` exposes only the lowest pending wave whose dependencies are healthy. Health observations are exact-SHA bound. Partial node loss removes only replicas hosted on the unavailable node; unrelated services preserve their state. A service with no effective healthy replicas transitions to `ROLLBACK_REQUIRED`, and rollback evidence must identify the exact failed SHA.

Credential revocation stores only an opaque ref and blocks only services that explicitly declare it. Restoring the ref recomputes health from retained evidence and current node loss. Product-operations snapshots do not contain raw secret material, protected-store handles, or active credential leases; the existing `CredentialBroker` remains authoritative for protected-store generation revocation and lease invalidation.

Maintenance side effects cross only `ProductOperationsPort.apply()` / `inspect()`. An explicit `approval_ref` is required before `apply()` is called. An uncertain result enters `PAUSED` and is reconciled through `inspect()` instead of blindly replaying the side effect. Automated tests use only a deterministic fake port.

The focused scale fixture models a 60-service social/messenger-style product across three rollout waves and eight nodes. It checks deterministic wave fan-out, partial node-loss isolation, one credential family affecting exactly its ten declared dependents, exact-SHA provenance, rollback isolation, and exact snapshot equality after coordinator restart. This complements the integrated scale suite that already covers up to 100 independently buildable components and repeated durable checkpoint/restart waves.

Acceptance for this batch requires Core CI Ruff/compile/full tests, M12 pre-human compatibility, and a final current-main/exact-head compatibility check before merge. No real node, provider, cloud, SSH, hosting, credential, or production action is performed by this batch. `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.
