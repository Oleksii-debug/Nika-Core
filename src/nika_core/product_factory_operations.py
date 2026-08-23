from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from .product_factory_operations_contracts import (
    DeployableService,
    MaintenanceAction,
    MaintenanceApprovalAuthorityPort,
    MaintenanceRequest,
    MaintenanceResult,
    MaintenanceState,
    ProductOperationsError,
    ProductOperationsPort,
    RollbackObservation,
    ServiceHealth,
    ServiceObservation,
)


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    service: DeployableService
    health: ServiceHealth = ServiceHealth.PENDING
    maintenance: MaintenanceState = MaintenanceState.IDLE
    observation: ServiceObservation | None = None
    rollback: RollbackObservation | None = None
    blocked_credentials: tuple[str, ...] = ()
    node_loss: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaintenanceRecord:
    request: MaintenanceRequest
    result: MaintenanceResult
    reconciled: bool = False

    def __post_init__(self) -> None:
        if type(self.reconciled) is not bool:
            raise ProductOperationsError("maintenance reconciled flag must be boolean")


@dataclass(frozen=True, slots=True)
class ProductOperationsSnapshot:
    project_id: str
    services: tuple[ServiceRecord, ...]
    maintenance_records: tuple[MaintenanceRecord, ...]
    revoked_credentials: tuple[str, ...]
    unavailable_nodes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectHealthSummary:
    project_id: str
    healthy: tuple[str, ...]
    degraded: tuple[str, ...]
    failed: tuple[str, ...]
    blocked: tuple[str, ...]
    rollback_required: tuple[str, ...]
    pending: tuple[str, ...]

    @property
    def release_ready(self) -> bool:
        return not (
            self.degraded
            or self.failed
            or self.blocked
            or self.rollback_required
            or self.pending
        )


@dataclass(slots=True)
class ProductOperationsCoordinator:
    project_id: str
    port: ProductOperationsPort | None = None
    approval_authority: MaintenanceApprovalAuthorityPort | None = None
    _services: dict[str, ServiceRecord] = field(default_factory=dict, init=False, repr=False)
    _maintenance: dict[str, MaintenanceRecord] = field(default_factory=dict, init=False, repr=False)
    _revoked: set[str] = field(default_factory=set, init=False, repr=False)
    _down_nodes: set[str] = field(default_factory=set, init=False, repr=False)
    _maintenance_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ProductOperationsError("project_id must not be empty")

    def register(self, service: DeployableService) -> ServiceRecord:
        if service.project_id != self.project_id:
            raise ProductOperationsError("service belongs to another project")
        existing = self._services.get(service.service_id)
        if existing is not None:
            if existing.service != service:
                raise ProductOperationsError("service id conflicts with prior payload")
            return existing
        for dependency in service.dependencies:
            prior = self._services.get(dependency)
            if prior is None or prior.service.wave >= service.wave:
                raise ProductOperationsError(
                    "dependency must be a registered earlier-wave service"
                )
        blocked = tuple(sorted(set(service.credential_refs) & self._revoked))
        record = ServiceRecord(
            service,
            ServiceHealth.BLOCKED if blocked else ServiceHealth.PENDING,
            blocked_credentials=blocked,
            node_loss=self._loss(service),
        )
        self._services[service.service_id] = record
        return record

    def ready_services(self) -> tuple[DeployableService, ...]:
        candidates = [
            record
            for record in self._services.values()
            if record.health is ServiceHealth.PENDING
        ]
        if not candidates:
            return ()
        wave = min(record.service.wave for record in candidates)
        return tuple(
            record.service
            for record in sorted(candidates, key=lambda item: item.service.service_id)
            if record.service.wave == wave
            and all(
                self._services[dependency].health is ServiceHealth.HEALTHY
                for dependency in record.service.dependencies
            )
        )

    def record_observation(self, observation: ServiceObservation) -> ServiceRecord:
        record = self._require(observation.service_id)
        if observation.release_sha != record.service.release_sha:
            raise ProductOperationsError("service observation release SHA mismatch")
        known = {replica.replica_id for replica in record.service.replicas}
        seen = set(observation.healthy_replica_ids) | set(observation.failed_replica_ids)
        if not seen <= known:
            raise ProductOperationsError("service observation references unknown replica")
        if record.observation is not None:
            if observation.observed_at < record.observation.observed_at:
                raise ProductOperationsError("service observation cannot rewind evidence time")
            if observation.observed_at == record.observation.observed_at:
                if observation != record.observation:
                    raise ProductOperationsError(
                        "service observation timestamp conflicts with prior evidence"
                    )
                return record
        if record.rollback is not None:
            raise ProductOperationsError(
                "service observation cannot advance after terminal rollback evidence"
            )
        updated = ServiceRecord(
            record.service,
            self._health(record, observation),
            record.maintenance,
            observation,
            record.rollback,
            record.blocked_credentials,
            self._loss(record.service),
        )
        self._services[record.service.service_id] = updated
        return updated

    def record_node_availability(self, node_id: str, *, available: bool) -> None:
        if not node_id.strip():
            raise ProductOperationsError("node_id must not be empty")
        if available:
            self._down_nodes.discard(node_id)
        else:
            self._down_nodes.add(node_id)
        for service_id, record in tuple(self._services.items()):
            health = (
                record.health
                if record.observation is None
                else self._health(record, record.observation)
            )
            self._services[service_id] = ServiceRecord(
                record.service,
                health,
                record.maintenance,
                record.observation,
                record.rollback,
                record.blocked_credentials,
                self._loss(record.service),
            )

    def revoke_credential(self, credential_ref: str) -> tuple[str, ...]:
        if not credential_ref.strip():
            raise ProductOperationsError("credential_ref must not be empty")
        self._revoked.add(credential_ref)
        affected: list[str] = []
        for service_id, record in tuple(self._services.items()):
            if credential_ref not in record.service.credential_refs:
                continue
            blocked = tuple(sorted(set(record.blocked_credentials) | {credential_ref}))
            self._services[service_id] = ServiceRecord(
                record.service,
                ServiceHealth.BLOCKED,
                record.maintenance,
                record.observation,
                record.rollback,
                blocked,
                record.node_loss,
            )
            affected.append(service_id)
        return tuple(sorted(affected))

    def restore_credential(self, credential_ref: str) -> tuple[str, ...]:
        self._revoked.discard(credential_ref)
        affected: list[str] = []
        for service_id, record in tuple(self._services.items()):
            if credential_ref not in record.blocked_credentials:
                continue
            blocked = tuple(
                value for value in record.blocked_credentials if value != credential_ref
            )
            probe = ServiceRecord(
                record.service,
                observation=record.observation,
                rollback=record.rollback,
                blocked_credentials=blocked,
                node_loss=record.node_loss,
            )
            health = ServiceHealth.BLOCKED if blocked else ServiceHealth.PENDING
            if not blocked and record.observation is not None:
                health = self._health(probe, record.observation)
            self._services[service_id] = ServiceRecord(
                record.service,
                health,
                record.maintenance,
                record.observation,
                record.rollback,
                blocked,
                record.node_loss,
            )
            affected.append(service_id)
        return tuple(sorted(affected))

    def record_rollback(self, observation: RollbackObservation) -> ServiceRecord:
        record = self._require(observation.service_id)
        if observation.failed_release_sha != record.service.release_sha:
            raise ProductOperationsError("rollback failed release SHA mismatch")
        if record.rollback is not None:
            if record.rollback != observation:
                raise ProductOperationsError("rollback evidence conflicts with prior payload")
            return record
        if record.observation is not None and observation.observed_at < record.observation.observed_at:
            raise ProductOperationsError("rollback evidence cannot predate service observation")
        if record.health is not ServiceHealth.ROLLBACK_REQUIRED:
            raise ProductOperationsError(
                "rollback evidence is not expected for service state"
            )
        updated = ServiceRecord(
            record.service,
            ServiceHealth.ROLLED_BACK
            if observation.succeeded
            else ServiceHealth.FAILED,
            record.maintenance,
            record.observation,
            observation,
            record.blocked_credentials,
            record.node_loss,
        )
        self._services[record.service.service_id] = updated
        return updated

    def request_maintenance(self, request: MaintenanceRequest) -> MaintenanceRecord:
        with self._maintenance_lock:
            record = self._require(request.service_id)
            existing = self._maintenance.get(request.request_id)
            if existing is not None:
                if existing.request != request:
                    raise ProductOperationsError(
                        "maintenance request id conflicts with prior payload"
                    )
                return existing
            if self.port is None or request.approval_ref is None:
                raise ProductOperationsError(
                    "maintenance side effect requires configured port and explicit approval"
                )
            self._validate_maintenance_authority(record, request)
            result = self.port.apply(request)
            if not isinstance(result, MaintenanceResult):
                raise ProductOperationsError(
                    "maintenance port returned invalid result evidence"
                )
            saved = MaintenanceRecord(request, result)
            self._maintenance[request.request_id] = saved
            self._services[request.service_id] = ServiceRecord(
                record.service,
                record.health,
                _maintenance_state(request.action, result),
                record.observation,
                record.rollback,
                record.blocked_credentials,
                record.node_loss,
            )
            return saved

    def reconcile_maintenance(self, request_id: str) -> MaintenanceRecord:
        with self._maintenance_lock:
            if request_id not in self._maintenance:
                raise ProductOperationsError("unknown maintenance request")
            current = self._maintenance[request_id]
            if not current.result.uncertain:
                return current
            if self.port is None:
                raise ProductOperationsError("maintenance side-effect port is not configured")
            record = self._require(current.request.service_id)
            self._validate_maintenance_authority(record, current.request)
            result = self.port.inspect(current.request)
            if not isinstance(result, MaintenanceResult):
                raise ProductOperationsError(
                    "maintenance port returned invalid inspection evidence"
                )
            saved = MaintenanceRecord(current.request, result, reconciled=True)
            self._maintenance[request_id] = saved
            self._services[record.service.service_id] = ServiceRecord(
                record.service,
                record.health,
                _maintenance_state(current.request.action, result),
                record.observation,
                record.rollback,
                record.blocked_credentials,
                record.node_loss,
            )
            return saved

    def health_summary(self) -> ProjectHealthSummary:
        bucket = {state: [] for state in ServiceHealth}
        for service_id, record in sorted(self._services.items()):
            bucket[record.health].append(service_id)
        return ProjectHealthSummary(
            self.project_id,
            tuple(bucket[ServiceHealth.HEALTHY])
            + tuple(bucket[ServiceHealth.ROLLED_BACK]),
            tuple(bucket[ServiceHealth.DEGRADED]),
            tuple(bucket[ServiceHealth.FAILED]),
            tuple(bucket[ServiceHealth.BLOCKED]),
            tuple(bucket[ServiceHealth.ROLLBACK_REQUIRED]),
            tuple(bucket[ServiceHealth.PENDING]),
        )

    def snapshot(self) -> ProductOperationsSnapshot:
        return ProductOperationsSnapshot(
            self.project_id,
            tuple(self._services[key] for key in sorted(self._services)),
            tuple(self._maintenance[key] for key in sorted(self._maintenance)),
            tuple(sorted(self._revoked)),
            tuple(sorted(self._down_nodes)),
        )

    def restore(self, snapshot: ProductOperationsSnapshot) -> None:
        if snapshot.project_id != self.project_id:
            raise ProductOperationsError("operations snapshot belongs to another project")
        ids = [record.service.service_id for record in snapshot.services]
        if len(ids) != len(set(ids)) or any(
            record.service.project_id != self.project_id for record in snapshot.services
        ):
            raise ProductOperationsError(
                "operations snapshot service identities are invalid"
            )
        services = {record.service.service_id: record for record in snapshot.services}
        for record in snapshot.services:
            for dependency in record.service.dependencies:
                prior = services.get(dependency)
                if prior is None or prior.service.wave >= record.service.wave:
                    raise ProductOperationsError(
                        "operations snapshot dependency is missing or not an earlier wave"
                    )

        revoked = self._validated_snapshot_refs(
            snapshot.revoked_credentials,
            "revoked credential",
        )
        down_nodes = self._validated_snapshot_refs(
            snapshot.unavailable_nodes,
            "unavailable node",
        )
        for record in snapshot.services:
            self._validate_restored_service(record, revoked, down_nodes)

        request_ids = [
            record.request.request_id for record in snapshot.maintenance_records
        ]
        if len(request_ids) != len(set(request_ids)):
            raise ProductOperationsError(
                "operations snapshot contains duplicate maintenance identities"
            )
        maintenance_by_service: dict[str, list[MaintenanceRecord]] = {}
        for maintenance in snapshot.maintenance_records:
            service = services.get(maintenance.request.service_id)
            if service is None:
                raise ProductOperationsError(
                    "operations snapshot maintenance references unknown service"
                )
            if maintenance.request.approval_ref is None:
                raise ProductOperationsError(
                    "operations snapshot maintenance lacks durable approval evidence"
                )
            self._validate_maintenance_authority(service, maintenance.request)
            maintenance_by_service.setdefault(maintenance.request.service_id, []).append(
                maintenance
            )

        for service_id, record in services.items():
            related = maintenance_by_service.get(service_id, [])
            if not related:
                if record.maintenance is not MaintenanceState.IDLE:
                    raise ProductOperationsError(
                        "operations snapshot maintenance state lacks durable request evidence"
                    )
                continue
            possible_states = {
                _maintenance_state(item.request.action, item.result) for item in related
            }
            if record.maintenance not in possible_states:
                raise ProductOperationsError(
                    "operations snapshot maintenance state is not backed by result evidence"
                )

        self._services = services
        self._maintenance = {
            record.request.request_id: record
            for record in snapshot.maintenance_records
        }
        self._revoked = revoked
        self._down_nodes = down_nodes

    def _require(self, service_id: str) -> ServiceRecord:
        try:
            return self._services[service_id]
        except KeyError as exc:
            raise ProductOperationsError("unknown deployable service") from exc

    def _loss(self, service: DeployableService) -> tuple[str, ...]:
        return self._loss_for(service, self._down_nodes)

    @staticmethod
    def _loss_for(service: DeployableService, down_nodes: set[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                replica.replica_id
                for replica in service.replicas
                if replica.node_id in down_nodes
            )
        )

    def _health(
        self,
        record: ServiceRecord,
        observation: ServiceObservation | None,
    ) -> ServiceHealth:
        return self._health_for(record, observation, self._down_nodes)

    @classmethod
    def _health_for(
        cls,
        record: ServiceRecord,
        observation: ServiceObservation | None,
        down_nodes: set[str],
    ) -> ServiceHealth:
        if record.blocked_credentials:
            return ServiceHealth.BLOCKED
        if record.rollback is not None:
            return ServiceHealth.ROLLED_BACK if record.rollback.succeeded else ServiceHealth.FAILED
        assert observation is not None
        loss = set(cls._loss_for(record.service, down_nodes))
        healthy = set(observation.healthy_replica_ids) - loss
        failed = set(observation.failed_replica_ids) | loss
        if len(healthy) >= record.service.min_healthy_replicas:
            if failed or len(healthy) < len(record.service.replicas):
                return ServiceHealth.DEGRADED
            return ServiceHealth.HEALTHY
        return ServiceHealth.DEGRADED if healthy else ServiceHealth.ROLLBACK_REQUIRED

    @staticmethod
    def _known_maintenance_evidence(record: ServiceRecord) -> set[str]:
        refs: set[str] = set()
        if record.observation is not None:
            refs.update(record.observation.evidence_refs)
        if record.rollback is not None:
            refs.update(record.rollback.evidence_refs)
        return refs

    def _validate_maintenance_authority(
        self,
        record: ServiceRecord,
        request: MaintenanceRequest,
    ) -> None:
        known = self._known_maintenance_evidence(record)
        if not known:
            raise ProductOperationsError(
                "maintenance requires approved service health/rollback evidence"
            )
        if not set(request.evidence_refs) <= known:
            raise ProductOperationsError(
                "maintenance evidence is not bound to the requested service"
            )
        if request.approval_ref is None or self.approval_authority is None:
            raise ProductOperationsError(
                "maintenance requires host-verified trusted approval authority"
            )
        try:
            approved = self.approval_authority.verify(
                project_id=self.project_id,
                service=record.service,
                request=request,
            )
        except Exception as exc:  # noqa: BLE001 - trusted boundary must fail closed
            raise ProductOperationsError(
                "maintenance trusted approval authority verification failed"
            ) from exc
        if approved is not True:
            raise ProductOperationsError(
                "maintenance approval is not authorized for exact service/release/request"
            )

    @classmethod
    def _validate_restored_service(
        cls,
        record: ServiceRecord,
        revoked: set[str],
        down_nodes: set[str],
    ) -> None:
        if not isinstance(record.health, ServiceHealth) or not isinstance(
            record.maintenance,
            MaintenanceState,
        ):
            raise ProductOperationsError("operations snapshot service state is invalid")
        expected_blocked = tuple(sorted(set(record.service.credential_refs) & revoked))
        if record.blocked_credentials != expected_blocked:
            raise ProductOperationsError(
                "operations snapshot blocked credential lineage is invalid"
            )
        expected_loss = cls._loss_for(record.service, down_nodes)
        if record.node_loss != expected_loss:
            raise ProductOperationsError("operations snapshot node-loss lineage is invalid")
        observation = record.observation
        if observation is not None:
            if (
                observation.service_id != record.service.service_id
                or observation.release_sha != record.service.release_sha
            ):
                raise ProductOperationsError(
                    "operations snapshot service observation identity is invalid"
                )
            known_replicas = {replica.replica_id for replica in record.service.replicas}
            observed_replicas = set(observation.healthy_replica_ids) | set(
                observation.failed_replica_ids
            )
            if not observed_replicas <= known_replicas:
                raise ProductOperationsError(
                    "operations snapshot observation references unknown replica"
                )
        rollback = record.rollback
        if rollback is not None and (
            rollback.service_id != record.service.service_id
            or rollback.failed_release_sha != record.service.release_sha
            or observation is None
            or rollback.observed_at < observation.observed_at
        ):
            raise ProductOperationsError(
                "operations snapshot rollback evidence identity is invalid"
            )
        if expected_blocked:
            expected_health = ServiceHealth.BLOCKED
        elif rollback is not None:
            expected_health = (
                ServiceHealth.ROLLED_BACK if rollback.succeeded else ServiceHealth.FAILED
            )
        elif observation is None:
            expected_health = ServiceHealth.PENDING
        else:
            probe = ServiceRecord(
                record.service,
                observation=observation,
                blocked_credentials=expected_blocked,
                node_loss=expected_loss,
            )
            expected_health = cls._health_for(probe, observation, down_nodes)
        if record.health is not expected_health:
            raise ProductOperationsError(
                "operations snapshot service health is not derivable from durable evidence"
            )

    @staticmethod
    def _validated_snapshot_refs(values: tuple[str, ...], label: str) -> set[str]:
        if any(type(value) is not str or not value.strip() for value in values):
            raise ProductOperationsError(f"operations snapshot {label} identity is invalid")
        if len(values) != len(set(values)):
            raise ProductOperationsError(f"operations snapshot contains duplicate {label} ids")
        return set(values)


def _maintenance_state(
    action: MaintenanceAction,
    result: MaintenanceResult,
) -> MaintenanceState:
    if result.uncertain:
        return MaintenanceState.PAUSED
    if not result.applied:
        return MaintenanceState.IDLE
    return {
        MaintenanceAction.DRAIN: MaintenanceState.DRAINING,
        MaintenanceAction.RESTART: MaintenanceState.RESTARTING,
        MaintenanceAction.RESUME: MaintenanceState.IDLE,
        MaintenanceAction.VERIFY: MaintenanceState.VERIFYING,
    }[action]
