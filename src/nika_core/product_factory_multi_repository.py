from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)
from nika_core.product_factory_program_host import (
    ProductFactoryProgramHost,
    ProductFactoryProgramWorkerPort,
    ProgramWorkOutcome,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import ProductProject

_GRAPH_SCHEMA = "nika-product-factory-repository-graph-v1"
_GRAPH_STAGE = "product_factory.repository_graph.v1"
_LINEAGE_SCHEMA = "nika-product-factory-repair-lineage-v1"
_LINEAGE_STAGE = "product_factory.repair_lineage.v1"
_HOST_KIND = "product_factory"
_GRAPH_AUTHORITY_KEY = "repository_graph_authority_fingerprint"


class MultiRepositoryExecutionError(ValueError):
    """Raised when durable multi-repository execution cannot proceed safely."""


class RepositoryGraphIntegrityError(MultiRepositoryExecutionError):
    """Raised when persisted repository graph authority is corrupt or inconsistent."""


class RepairLineageError(MultiRepositoryExecutionError):
    """Raised when a repair generation is not backed by exact durable lineage."""


@dataclass(frozen=True, slots=True, order=True)
class VersionedDependencyEdge:
    graph_version: int
    component_id: str
    depends_on_component_id: str

    def __post_init__(self) -> None:
        if self.graph_version < 1:
            raise MultiRepositoryExecutionError("graph_version must be positive")
        if not self.component_id.strip() or not self.depends_on_component_id.strip():
            raise MultiRepositoryExecutionError("dependency edge identity must not be empty")
        if self.component_id == self.depends_on_component_id:
            raise MultiRepositoryExecutionError("component cannot depend on itself")


@dataclass(frozen=True, slots=True)
class RepositoryGraphAuthority:
    checkpoint_id: str
    project_id: str
    spec_version: int
    row_version: int
    graph_version: int
    graph_digest: str
    graph: ProductRepositoryGraph
    dependency_edges: tuple[VersionedDependencyEdge, ...]


@dataclass(frozen=True, slots=True)
class RepairLineageIntent:
    lineage_id: str
    project_id: str
    spec_version: int
    row_version: int
    graph_digest: str
    component_id: str
    from_attempt: int
    from_work_id: str
    from_result_sha: str
    to_attempt: int
    to_work_id: str
    to_base_sha: str
    reason: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.lineage_id,
                self.project_id,
                self.graph_digest,
                self.component_id,
                self.from_work_id,
                self.to_work_id,
                self.reason,
            )
        ):
            raise RepairLineageError("repair lineage identity and reason must not be empty")
        if self.spec_version < 1 or self.row_version < 0:
            raise RepairLineageError("repair lineage ProductProject version is invalid")
        if self.from_attempt < 1 or self.to_attempt != self.from_attempt + 1:
            raise RepairLineageError("repair lineage attempts must advance exactly one generation")
        _validate_sha(self.from_result_sha, "from_result_sha")
        _validate_sha(self.to_base_sha, "to_base_sha")
        if self.from_result_sha != self.to_base_sha:
            raise RepairLineageError("repair base must equal the exact prior result SHA")


@dataclass(slots=True)
class MultiRepositoryExecutionState:
    authority: RepositoryGraphAuthority
    binding: ProductProjectCoordinatorBinding
    coordinator: ProductFactoryCoordinator


@dataclass(slots=True)
class MultiRepositoryProductFactoryHost:
    """Thin durable composition over ProductRepositoryGraph and ProductFactoryProgramHost.

    This class deliberately does not create a second scheduler, worker runtime, repository
    abstraction or lease database. The graph remains the canonical ownership/dependency
    contract; coordinator RUNNING state is the durable source for active ownership leases;
    ProductFactoryProgramHost owns worker dispatch/recovery/checkpoint ordering.
    """

    store: SQLiteStore
    worker: ProductFactoryProgramWorkerPort
    _program: ProductFactoryProgramHost = field(init=False, repr=False)
    _coordinator_checkpoints: ProductFactoryCheckpointHost = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._program = ProductFactoryProgramHost(self.store, self.worker)
        self._coordinator_checkpoints = ProductFactoryCheckpointHost(self.store)

    def initialize(
        self,
        *,
        host_task_id: str,
        project: ProductProject,
        graph: ProductRepositoryGraph,
        graph_version: int,
        base_shas: Mapping[str, str],
        component_goals: Mapping[str, str],
        permission_ceiling: frozenset[str],
    ) -> MultiRepositoryExecutionState:
        """Bind one immutable graph authority and create the first durable work checkpoint."""

        binding = ProductProjectCoordinatorBinding(project, graph)
        authority = self._bind_graph(
            host_task_id=host_task_id,
            project=project,
            graph=graph,
            graph_version=graph_version,
        )
        coordinator = binding.plan(
            base_shas=dict(base_shas),
            component_goals=dict(component_goals),
            permission_ceiling=permission_ceiling,
        )
        self._coordinator_checkpoints.save(
            host_task_id=host_task_id,
            checkpoint=binding.checkpoint(coordinator),
        )
        state = MultiRepositoryExecutionState(authority, binding, coordinator)
        self._assert_state(host_task_id=host_task_id, state=state)
        return state

    def restore(
        self,
        *,
        host_task_id: str,
        project: ProductProject,
    ) -> MultiRepositoryExecutionState:
        """Reconstruct exact graph + coordinator state without caller-supplied graph bytes."""

        authority = self._load_graph(host_task_id=host_task_id, project=project)
        binding = ProductProjectCoordinatorBinding(project, authority.graph)
        coordinator = self._program.restore_latest(
            host_task_id=host_task_id,
            binding=binding,
        )
        state = MultiRepositoryExecutionState(authority, binding, coordinator)
        self._assert_state(host_task_id=host_task_id, state=state)
        self._validate_repair_lineage(
            host_task_id=host_task_id,
            state=state,
        )
        return state

    async def dispatch_ready(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        active_leases: Iterable[OwnershipLease] = (),
        max_parallel: int = 4,
        max_count: int = 32,
    ) -> tuple[ProgramWorkOutcome, ...]:
        """Dispatch only work whose graph ownership can be granted without overlap."""

        self._assert_state(host_task_id=host_task_id, state=state)
        self._validate_dispatch_ownership(
            state=state,
            active_leases=tuple(active_leases),
            max_count=max_count,
        )
        return await self._program.dispatch_ready(
            host_task_id=host_task_id,
            binding=state.binding,
            coordinator=state.coordinator,
            max_parallel=max_parallel,
            max_count=max_count,
        )

    async def recover_running(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        max_parallel: int = 4,
    ) -> tuple[ProgramWorkOutcome, ...]:
        self._assert_state(host_task_id=host_task_id, state=state)
        self._validate_running_ownership(state)
        return await self._program.recover_running(
            host_task_id=host_task_id,
            binding=state.binding,
            coordinator=state.coordinator,
            max_parallel=max_parallel,
        )

    def review_and_checkpoint(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        component_id: str,
        decision: ReviewDecision,
    ) -> WorkRecord:
        self._assert_state(host_task_id=host_task_id, state=state)
        return self._program.review_and_checkpoint(
            host_task_id=host_task_id,
            binding=state.binding,
            coordinator=state.coordinator,
            component_id=component_id,
            decision=decision,
        )

    def prepare_repair_and_checkpoint(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        component_id: str,
        reason: str,
    ) -> tuple[ComponentWorkRequest, RepairLineageIntent]:
        """Advance repair from exact prior result SHA through a durable lineage intent.

        The caller cannot nominate a new base. The next request is previewed through the
        canonical coordinator, restored in-memory, then its exact transition is persisted
        before ProductFactoryProgramHost durably creates the next READY generation.
        """

        self._assert_state(host_task_id=host_task_id, state=state)
        if not reason.strip():
            raise RepairLineageError("repair reason must not be empty")
        record = _record_for_component(state.coordinator, component_id)
        if record.state is not WorkState.REPAIR_REQUIRED or record.result is None:
            raise RepairLineageError("component is not awaiting repair with exact result evidence")

        before = state.coordinator.snapshot()
        try:
            preview = state.coordinator.prepare_repair(
                component_id,
                base_sha=record.result.result_sha,
                reason=reason,
            )
        finally:
            state.coordinator.restore(before)

        intent = self._lineage_intent(
            state=state,
            previous=record,
            preview=preview,
            reason=reason,
        )
        intent = self._persist_lineage_intent(
            host_task_id=host_task_id,
            state=state,
            intent=intent,
        )

        request = self._program.prepare_repair_and_checkpoint(
            host_task_id=host_task_id,
            binding=state.binding,
            coordinator=state.coordinator,
            component_id=component_id,
            base_sha=intent.to_base_sha,
            reason=reason,
        )
        if (
            request.work_id != intent.to_work_id
            or request.base_sha != intent.to_base_sha
            or request.attempt != intent.to_attempt
        ):
            raise RepairLineageError(
                "canonical repair request disagrees with durable lineage intent"
            )
        self._validate_repair_lineage(host_task_id=host_task_id, state=state)
        return request, intent

    def running_ownership_leases(
        self,
        state: MultiRepositoryExecutionState,
    ) -> tuple[OwnershipLease, ...]:
        records = state.coordinator.snapshot().records
        return tuple(
            _ownership_lease(record.request)
            for record in records
            if record.state is WorkState.RUNNING
        )

    def repair_lineage(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
    ) -> tuple[RepairLineageIntent, ...]:
        self._assert_state(host_task_id=host_task_id, state=state)
        lineage = self._load_lineage_intents(host_task_id=host_task_id, state=state)
        self._validate_lineage_records(state=state, lineage=lineage)
        return lineage

    def _bind_graph(
        self,
        *,
        host_task_id: str,
        project: ProductProject,
        graph: ProductRepositoryGraph,
        graph_version: int,
    ) -> RepositoryGraphAuthority:
        if graph_version < 1:
            raise MultiRepositoryExecutionError("graph_version must be positive")
        if graph.project_id != project.project_id:
            raise MultiRepositoryExecutionError(
                "repository graph does not belong to ProductProject"
            )
        ProductProjectCoordinatorBinding(project, graph)

        graph_payload = _encode_graph(graph)
        graph_digest = _sha256(_canonical(graph_payload))
        edges = _dependency_edges(graph, graph_version)
        payload = {
            "schema": _GRAPH_SCHEMA,
            "project_id": project.project_id,
            "spec_version": project.spec_version,
            "row_version": project.row_version,
            "graph_version": graph_version,
            "graph_digest": graph_digest,
            "graph": graph_payload,
            "dependency_edges": [
                {
                    "graph_version": edge.graph_version,
                    "component_id": edge.component_id,
                    "depends_on_component_id": edge.depends_on_component_id,
                }
                for edge in edges
            ],
        }
        canonical = _canonical(payload)
        checksum = _sha256(canonical)
        checkpoint_id = f"pf-repository-graph:{_sha256(host_task_id + ':' + checksum)}"
        authority_fingerprint = _graph_authority_fingerprint(
            project=project,
            graph_version=graph_version,
            graph_digest=graph_digest,
        )

        with self.store.connection() as conn:
            host_payload = self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=project.project_id,
            )
            rows = conn.execute(
                """
                SELECT checkpoint_id, payload_json, checksum_sha256
                FROM checkpoints
                WHERE task_id = ? AND stage = ?
                ORDER BY created_at, checkpoint_id
                """,
                (host_task_id, _GRAPH_STAGE),
            ).fetchall()
            host_authority = host_payload.get(_GRAPH_AUTHORITY_KEY)
            if host_authority is None:
                if rows:
                    raise RepositoryGraphIntegrityError(
                        "legacy repository graph checkpoint has no host authority"
                    )
                host_payload = dict(host_payload)
                host_payload[_GRAPH_AUTHORITY_KEY] = authority_fingerprint
                conn.execute(
                    "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
                    (_canonical(host_payload), host_task_id),
                )
                self._audit(
                    conn,
                    event_type="product_factory.repository_graph_authority_bound",
                    project_id=project.project_id,
                    payload={
                        "host_task_id": host_task_id,
                        "graph_version": graph_version,
                        "graph_digest": graph_digest,
                        "authority_fingerprint": authority_fingerprint,
                    },
                )
            elif host_authority != authority_fingerprint:
                raise RepositoryGraphIntegrityError(
                    "host task repository graph authority does not match candidate graph"
                )
            if len(rows) > 1:
                raise RepositoryGraphIntegrityError(
                    "host task has multiple repository graph authorities"
                )
            if rows:
                authority = self._graph_authority_from_row(rows[0], project=project)
                if (
                    authority.graph_digest != graph_digest
                    or authority.graph_version != graph_version
                    or authority.spec_version != project.spec_version
                    or authority.row_version != project.row_version
                ):
                    raise RepositoryGraphIntegrityError(
                        "host task repository graph authority cannot be silently replaced"
                    )
                return authority

            now = datetime.now(UTC).isoformat()
            try:
                conn.execute(
                    """
                    INSERT INTO checkpoints(
                        checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint_id,
                        host_task_id,
                        _GRAPH_STAGE,
                        canonical,
                        checksum,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryGraphIntegrityError(
                    "repository graph checkpoint identity already exists"
                ) from exc
            self._audit(
                conn,
                event_type="product_factory.repository_graph_bound",
                project_id=project.project_id,
                payload={
                    "host_task_id": host_task_id,
                    "checkpoint_id": checkpoint_id,
                    "graph_version": graph_version,
                    "graph_digest": graph_digest,
                    "repository_count": len(graph.repositories),
                    "component_count": len(graph.components),
                },
            )

        return RepositoryGraphAuthority(
            checkpoint_id=checkpoint_id,
            project_id=project.project_id,
            spec_version=project.spec_version,
            row_version=project.row_version,
            graph_version=graph_version,
            graph_digest=graph_digest,
            graph=graph,
            dependency_edges=edges,
        )

    def _load_graph(
        self,
        *,
        host_task_id: str,
        project: ProductProject,
    ) -> RepositoryGraphAuthority:
        with self.store.connection() as conn:
            host_payload = self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=project.project_id,
            )
            rows = conn.execute(
                """
                SELECT checkpoint_id, payload_json, checksum_sha256
                FROM checkpoints
                WHERE task_id = ? AND stage = ?
                ORDER BY created_at, checkpoint_id
                """,
                (host_task_id, _GRAPH_STAGE),
            ).fetchall()
        if not rows:
            raise RepositoryGraphIntegrityError(
                "host task has no durable repository graph authority"
            )
        if len(rows) != 1:
            raise RepositoryGraphIntegrityError(
                "host task has multiple repository graph authorities"
            )
        authority = self._graph_authority_from_row(rows[0], project=project)
        expected = _graph_authority_fingerprint(
            project=project,
            graph_version=authority.graph_version,
            graph_digest=authority.graph_digest,
        )
        if host_payload.get(_GRAPH_AUTHORITY_KEY) != expected:
            raise RepositoryGraphIntegrityError(
                "host task repository graph authority is missing or mismatched"
            )
        return authority

    def _graph_authority_from_row(
        self,
        row: Any,
        *,
        project: ProductProject,
    ) -> RepositoryGraphAuthority:
        canonical = str(row["payload_json"])
        if _sha256(canonical) != row["checksum_sha256"]:
            raise RepositoryGraphIntegrityError("repository graph checkpoint checksum mismatch")
        try:
            payload = json.loads(canonical)
        except json.JSONDecodeError as exc:
            raise RepositoryGraphIntegrityError(
                "repository graph checkpoint is not valid JSON"
            ) from exc
        if payload.get("schema") != _GRAPH_SCHEMA:
            raise RepositoryGraphIntegrityError("repository graph checkpoint schema mismatch")
        if (
            payload.get("project_id") != project.project_id
            or payload.get("spec_version") != project.spec_version
            or payload.get("row_version") != project.row_version
        ):
            raise RepositoryGraphIntegrityError(
                "repository graph authority is stale for current ProductProject"
            )
        try:
            graph = _decode_graph(payload["graph"])
            graph_version = int(payload["graph_version"])
            edges = tuple(
                VersionedDependencyEdge(
                    graph_version=int(item["graph_version"]),
                    component_id=str(item["component_id"]),
                    depends_on_component_id=str(item["depends_on_component_id"]),
                )
                for item in payload["dependency_edges"]
            )
        except (AttributeError, KeyError, TypeError, ValueError, RepositoryGraphError) as exc:
            raise RepositoryGraphIntegrityError(
                "repository graph checkpoint payload is invalid"
            ) from exc
        graph_digest = _sha256(_canonical(_encode_graph(graph)))
        if payload.get("graph_digest") != graph_digest:
            raise RepositoryGraphIntegrityError("repository graph digest mismatch")
        expected_edges = _dependency_edges(graph, graph_version)
        if edges != expected_edges:
            raise RepositoryGraphIntegrityError(
                "versioned dependency edge evidence disagrees with repository graph"
            )
        ProductProjectCoordinatorBinding(project, graph)
        return RepositoryGraphAuthority(
            checkpoint_id=str(row["checkpoint_id"]),
            project_id=project.project_id,
            spec_version=project.spec_version,
            row_version=project.row_version,
            graph_version=graph_version,
            graph_digest=graph_digest,
            graph=graph,
            dependency_edges=edges,
        )

    def _lineage_intent(
        self,
        *,
        state: MultiRepositoryExecutionState,
        previous: WorkRecord,
        preview: ComponentWorkRequest,
        reason: str,
    ) -> RepairLineageIntent:
        payload = {
            "project_id": state.authority.project_id,
            "spec_version": state.authority.spec_version,
            "row_version": state.authority.row_version,
            "graph_digest": state.authority.graph_digest,
            "component_id": previous.request.component_id,
            "from_attempt": previous.request.attempt,
            "from_work_id": previous.request.work_id,
            "from_result_sha": previous.result.result_sha if previous.result else "",
            "to_attempt": preview.attempt,
            "to_work_id": preview.work_id,
            "to_base_sha": preview.base_sha,
            "reason": reason,
        }
        lineage_id = f"pf-repair-lineage:{_sha256(_canonical(payload))}"
        return RepairLineageIntent(lineage_id=lineage_id, **payload)

    def _persist_lineage_intent(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        intent: RepairLineageIntent,
    ) -> RepairLineageIntent:
        payload = _encode_lineage(intent)
        canonical = _canonical(payload)
        checksum = _sha256(canonical)
        with self.store.connection() as conn:
            self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=state.authority.project_id,
            )
            rows = conn.execute(
                """
                SELECT checkpoint_id, payload_json, checksum_sha256
                FROM checkpoints
                WHERE task_id = ? AND stage = ?
                ORDER BY created_at, checkpoint_id
                """,
                (host_task_id, _LINEAGE_STAGE),
            ).fetchall()
            for row in rows:
                current = self._lineage_from_row(row, state=state)
                if (
                    current.component_id == intent.component_id
                    and current.to_attempt == intent.to_attempt
                ):
                    if current != intent:
                        raise RepairLineageError(
                            "repair generation already has incompatible durable lineage"
                        )
                    return current

            now = datetime.now(UTC).isoformat()
            try:
                conn.execute(
                    """
                    INSERT INTO checkpoints(
                        checkpoint_id, task_id, stage, payload_json, checksum_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.lineage_id,
                        host_task_id,
                        _LINEAGE_STAGE,
                        canonical,
                        checksum,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RepairLineageError(
                    "repair lineage checkpoint identity already exists"
                ) from exc
            self._audit(
                conn,
                event_type="product_factory.repair_lineage_prepared",
                project_id=state.authority.project_id,
                payload={
                    "host_task_id": host_task_id,
                    "lineage_id": intent.lineage_id,
                    "component_id": intent.component_id,
                    "from_attempt": intent.from_attempt,
                    "to_attempt": intent.to_attempt,
                    "from_work_id": intent.from_work_id,
                    "to_work_id": intent.to_work_id,
                    "to_base_sha": intent.to_base_sha,
                },
            )
        return intent

    def _load_lineage_intents(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
    ) -> tuple[RepairLineageIntent, ...]:
        with self.store.connection() as conn:
            rows = conn.execute(
                """
                SELECT checkpoint_id, payload_json, checksum_sha256
                FROM checkpoints
                WHERE task_id = ? AND stage = ?
                ORDER BY created_at, checkpoint_id
                """,
                (host_task_id, _LINEAGE_STAGE),
            ).fetchall()
        intents = tuple(self._lineage_from_row(row, state=state) for row in rows)
        return tuple(
            sorted(
                intents,
                key=lambda item: (item.component_id, item.to_attempt, item.lineage_id),
            )
        )

    def _lineage_from_row(
        self,
        row: Any,
        *,
        state: MultiRepositoryExecutionState,
    ) -> RepairLineageIntent:
        canonical = str(row["payload_json"])
        if _sha256(canonical) != row["checksum_sha256"]:
            raise RepairLineageError("repair lineage checkpoint checksum mismatch")
        try:
            payload = json.loads(canonical)
        except json.JSONDecodeError as exc:
            raise RepairLineageError("repair lineage checkpoint is not valid JSON") from exc
        if payload.get("schema") != _LINEAGE_SCHEMA:
            raise RepairLineageError("repair lineage checkpoint schema mismatch")
        try:
            intent = RepairLineageIntent(
                lineage_id=str(payload["lineage_id"]),
                project_id=str(payload["project_id"]),
                spec_version=int(payload["spec_version"]),
                row_version=int(payload["row_version"]),
                graph_digest=str(payload["graph_digest"]),
                component_id=str(payload["component_id"]),
                from_attempt=int(payload["from_attempt"]),
                from_work_id=str(payload["from_work_id"]),
                from_result_sha=str(payload["from_result_sha"]),
                to_attempt=int(payload["to_attempt"]),
                to_work_id=str(payload["to_work_id"]),
                to_base_sha=str(payload["to_base_sha"]),
                reason=str(payload["reason"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RepairLineageError("repair lineage checkpoint payload is invalid") from exc
        if row["checkpoint_id"] != intent.lineage_id:
            raise RepairLineageError("repair lineage checkpoint identity mismatch")
        if (
            intent.project_id != state.authority.project_id
            or intent.spec_version != state.authority.spec_version
            or intent.row_version != state.authority.row_version
            or intent.graph_digest != state.authority.graph_digest
        ):
            raise RepairLineageError(
                "repair lineage does not belong to current ProductProject graph authority"
            )
        return intent

    def _validate_repair_lineage(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
    ) -> None:
        lineage = self._load_lineage_intents(host_task_id=host_task_id, state=state)
        self._validate_lineage_records(state=state, lineage=lineage)

    def _validate_lineage_records(
        self,
        *,
        state: MultiRepositoryExecutionState,
        lineage: tuple[RepairLineageIntent, ...],
    ) -> None:
        snapshot = state.coordinator.snapshot()
        if snapshot.trusted_plan is None:
            raise RepairLineageError("coordinator is missing immutable trusted plan")
        initial_by_component = {
            request.component_id: request for request in snapshot.trusted_plan
        }
        current_by_component = {
            record.request.component_id: record for record in snapshot.records
        }
        grouped: dict[str, list[RepairLineageIntent]] = {}
        seen_generation: set[tuple[str, int]] = set()
        for intent in lineage:
            key = (intent.component_id, intent.to_attempt)
            if key in seen_generation:
                raise RepairLineageError("repair generation has duplicate lineage intents")
            seen_generation.add(key)
            grouped.setdefault(intent.component_id, []).append(intent)

        for component_id, current in current_by_component.items():
            initial = initial_by_component[component_id]
            intents = sorted(grouped.pop(component_id, ()), key=lambda item: item.to_attempt)
            expected_from_work_id = initial.work_id
            expected_from_attempt = 1
            for intent in intents:
                if (
                    intent.from_attempt != expected_from_attempt
                    or intent.to_attempt != expected_from_attempt + 1
                    or intent.from_work_id != expected_from_work_id
                ):
                    raise RepairLineageError(
                        f"repair lineage chain is discontinuous for component {component_id}"
                    )
                if intent.to_base_sha != intent.from_result_sha:
                    raise RepairLineageError(
                        f"repair lineage base mismatch for component {component_id}"
                    )
                expected_from_work_id = intent.to_work_id
                expected_from_attempt = intent.to_attempt

            current_attempt = current.request.attempt
            if current_attempt > 1:
                if expected_from_attempt < current_attempt:
                    raise RepairLineageError(
                        f"component {component_id} advanced without durable repair lineage"
                    )
                applied = next(
                    (item for item in intents if item.to_attempt == current_attempt),
                    None,
                )
                if applied is None:
                    raise RepairLineageError(
                        f"component {component_id} current attempt has no durable lineage"
                    )
                if (
                    applied.to_work_id != current.request.work_id
                    or applied.to_base_sha != current.request.base_sha
                ):
                    raise RepairLineageError(
                        f"component {component_id} current request disagrees with lineage"
                    )

            if expected_from_attempt > current_attempt:
                if expected_from_attempt != current_attempt + 1:
                    raise RepairLineageError(
                        f"component {component_id} has lineage beyond one pending generation"
                    )
                pending = intents[-1]
                if (
                    current.state is not WorkState.REPAIR_REQUIRED
                    or current.result is None
                    or pending.from_work_id != current.request.work_id
                    or pending.from_result_sha != current.result.result_sha
                ):
                    raise RepairLineageError(
                        f"component {component_id} has invalid pending repair lineage"
                    )

        if grouped:
            unknown = ", ".join(sorted(grouped))
            raise RepairLineageError(
                f"repair lineage references unknown component(s): {unknown}"
            )

    def _validate_dispatch_ownership(
        self,
        *,
        state: MultiRepositoryExecutionState,
        active_leases: tuple[OwnershipLease, ...],
        max_count: int,
    ) -> None:
        if max_count <= 0:
            raise ValueError("max_count must be positive")
        running = self.running_ownership_leases(state)
        all_active = (*active_leases, *running)
        active_ids = [lease.lease_id for lease in all_active]
        if len(active_ids) != len(set(active_ids)):
            raise MultiRepositoryExecutionError("active ownership lease ids must be unique")

        selected: list[OwnershipLease] = []
        for request in state.coordinator.ready_requests()[:max_count]:
            candidate = _ownership_lease(request)
            try:
                assessment = state.authority.graph.assess_lease(
                    candidate,
                    (*all_active, *selected),
                )
            except RepositoryGraphError as exc:
                raise MultiRepositoryExecutionError(
                    f"ownership lease validation failed for {request.component_id}: {exc}"
                ) from exc
            if assessment.conflicts:
                details = "; ".join(
                    (
                        f"{item.repository_id}:{item.active_lease_id}:"
                        f"{item.path_a}<->{item.path_b}"
                    )
                    for item in assessment.conflicts
                )
                raise MultiRepositoryExecutionError(
                    f"component {request.component_id} has deterministic ownership conflict: "
                    f"{details}"
                )
            selected.append(candidate)

    def _validate_running_ownership(self, state: MultiRepositoryExecutionState) -> None:
        running = self.running_ownership_leases(state)
        accepted: list[OwnershipLease] = []
        for candidate in running:
            assessment = state.authority.graph.assess_lease(candidate, accepted)
            if assessment.conflicts:
                raise MultiRepositoryExecutionError(
                    "durable running work contains overlapping ownership leases"
                )
            accepted.append(candidate)

    def _assert_state(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
    ) -> None:
        project = state.binding.project
        if (
            state.authority.project_id != project.project_id
            or state.authority.spec_version != project.spec_version
            or state.authority.row_version != project.row_version
        ):
            raise MultiRepositoryExecutionError(
                "execution state is stale for current ProductProject"
            )
        if state.binding.graph is not state.authority.graph:
            raise MultiRepositoryExecutionError(
                "execution binding does not use durable repository graph authority"
            )
        if state.coordinator.graph is not state.authority.graph:
            raise MultiRepositoryExecutionError(
                "coordinator does not use durable repository graph authority"
            )
        with self.store.connection() as conn:
            self._require_host_task(
                conn,
                host_task_id=host_task_id,
                project_id=project.project_id,
            )

    @staticmethod
    def _require_host_task(
        conn: Any,
        *,
        host_task_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            (host_task_id,),
        ).fetchone()
        if row is None:
            raise MultiRepositoryExecutionError("Product Factory host task does not exist")
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            raise MultiRepositoryExecutionError(
                "Product Factory host task payload is corrupt"
            ) from exc
        if (
            payload.get("kind") != _HOST_KIND
            or payload.get("product_project_id") != project_id
        ):
            raise MultiRepositoryExecutionError(
                "host task is not bound to the expected ProductProject"
            )
        return payload

    @staticmethod
    def _audit(
        conn: Any,
        *,
        event_type: str,
        project_id: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_events(
                event_type, entity_type, entity_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                "product_project",
                project_id,
                _canonical(payload),
                datetime.now(UTC).isoformat(),
            ),
        )


def _record_for_component(
    coordinator: ProductFactoryCoordinator,
    component_id: str,
) -> WorkRecord:
    for record in coordinator.snapshot().records:
        if record.request.component_id == component_id:
            return record
    raise MultiRepositoryExecutionError(f"unknown component {component_id}")


def _ownership_lease(request: ComponentWorkRequest) -> OwnershipLease:
    return OwnershipLease(
        lease_id=f"pf-work:{request.work_id}",
        worker_id=request.work_id,
        component_ids=(request.component_id,),
        allowed_paths=request.allowed_paths,
    )


def _dependency_edges(
    graph: ProductRepositoryGraph,
    graph_version: int,
) -> tuple[VersionedDependencyEdge, ...]:
    if graph_version < 1:
        raise MultiRepositoryExecutionError("graph_version must be positive")
    return tuple(
        sorted(
            VersionedDependencyEdge(
                graph_version=graph_version,
                component_id=component.component_id,
                depends_on_component_id=dependency,
            )
            for component in graph.components
            for dependency in component.dependencies
        )
    )


def _encode_graph(graph: ProductRepositoryGraph) -> dict[str, Any]:
    return {
        "project_id": graph.project_id,
        "repositories": [asdict(repository) for repository in graph.repositories],
        "components": [asdict(component) for component in graph.components],
    }


def _decode_graph(payload: Mapping[str, Any]) -> ProductRepositoryGraph:
    repositories = tuple(
        RepositoryRef(**dict(item))
        for item in payload.get("repositories", ())
    )
    components = tuple(
        ProductComponent(
            **{
                **dict(item),
                "paths": tuple(item.get("paths", ())),
                "dependencies": tuple(item.get("dependencies", ())),
                "build_commands": tuple(
                    tuple(command) for command in item.get("build_commands", ())
                ),
                "test_commands": tuple(
                    tuple(command) for command in item.get("test_commands", ())
                ),
            }
        )
        for item in payload.get("components", ())
    )
    return ProductRepositoryGraph(
        project_id=str(payload["project_id"]),
        repositories=repositories,
        components=components,
    )


def _encode_lineage(intent: RepairLineageIntent) -> dict[str, Any]:
    return {
        "schema": _LINEAGE_SCHEMA,
        **asdict(intent),
    }


def _graph_authority_fingerprint(
    *,
    project: ProductProject,
    graph_version: int,
    graph_digest: str,
) -> str:
    return _sha256(
        _canonical(
            {
                "project_id": project.project_id,
                "spec_version": project.spec_version,
                "row_version": project.row_version,
                "graph_version": graph_version,
                "graph_digest": graph_digest,
            }
        )
    )


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value.casefold()):
        raise RepairLineageError(f"{label} must be a 40-character hexadecimal SHA")
