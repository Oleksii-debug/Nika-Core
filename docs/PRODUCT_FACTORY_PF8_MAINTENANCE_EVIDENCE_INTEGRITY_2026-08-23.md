# PF8 Product Operations — Maintenance Evidence Integrity

Status: MANUAL-DEV10 implementation/evidence contract.

## Collision and scope boundary

PF3 incident repair/release already landed before this work-run. Active PR #178 owns repeated-incident isolation in `product_factory_incident_contracts.py` and `product_factory_incidents.py`. The active DEV3 fleet-replacement branch owns fleet replacement/maintenance source. This batch therefore changes only the independent Product Operations maintenance evidence boundary and its tests.

## REUSE → ADAPT → CUSTOM (thin)

REUSE the existing `ProductOperationsCoordinator`, `ProductOperationsPort`, durable snapshots, service observations, rollback evidence, explicit approval reference and maintenance idempotency identity.

ADAPT maintenance authorization so an approved side effect must cite evidence already recorded for the exact service. An approval reference by itself is not sufficient to turn arbitrary text or another service's health evidence into a restart/drain/resume/verify action.

CUSTOM is limited to evidence-lineage validation, strict primitive identity checks and restart reconciliation. No shell path, permission bypass, provider-specific API or self-modifying production mechanism is added.

## Runtime invariants

- Maintenance requires a configured side-effect port and explicit approval, as before.
- Before calling the port, every `MaintenanceRequest.evidence_refs` item must be present in the requested service's recorded health or rollback evidence.
- A service with no approved health/rollback evidence cannot be maintained through this coordinator.
- Request-id replay remains idempotent and does not call the provider twice.
- Service observation timestamps cannot move backwards; a different payload at the same timestamp is rejected rather than overwriting evidence.
- Exact rollback evidence replay is idempotent; conflicting rollback evidence is rejected.
- Maintenance adapter results must cross the boundary as `MaintenanceResult`, with exact boolean flags and non-duplicate evidence references.

## Restart reconciliation

`restore()` validates the complete snapshot before replacing coordinator state. It re-derives and checks:

- project/service identities and earlier-wave dependencies;
- revoked credential identity and each service's exact blocked-credential set;
- unavailable-node identity and exact per-service replica loss;
- service observation release/service/replica binding;
- rollback service/release/timeline binding;
- service health from durable observation, credential, node-loss and rollback evidence;
- maintenance request uniqueness, target service, durable approval and evidence binding;
- maintenance state backed by at least one persisted result for that service.

Corruption therefore fails closed without partially replacing the coordinator's prior in-memory state.

## Scale and isolation evidence

Focused tests exercise 50 services with five independently maintained services and restart the snapshot, proving that maintenance state does not leak to the other 45 services. Existing 60-service Product Operations coverage remains the broad multi-service regression gate.

## Truth

This is automated engineering evidence only.

`HUMAN_TESTED=false`

`NVDA_VERIFIED=false`
