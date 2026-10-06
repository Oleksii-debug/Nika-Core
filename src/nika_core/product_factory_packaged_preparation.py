from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from uuid import NAMESPACE_URL, uuid5

from nika_core.kernel.task_queue import TaskPayloadCorruptionError, TaskQueue
from nika_core.product_factory_coordinator import ComponentWorkRequest, WorkState
from nika_core.product_factory_multi_repository import (
    MultiRepositoryExecutionState,
    MultiRepositoryProductFactoryHost,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)
from nika_core.product_project import ProductProject, ProductProjectRepository

PRODUCT_FACTORY_HOST_AGENT_ID = "product-factory"
_HOST_TASK_KIND = "product_factory"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class PackagedProductFactoryPreparationError(ValueError):
    """Trusted packaged Product Factory preparation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class PackagedProductFactoryExecutionPlan:
    """Immutable trusted inputs required before Product Factory execution may begin."""

    project_id: str
    expected_spec_version: int
    expected_row_version: int
    graph: ProductRepositoryGraph
    graph_version: int
    base_shas: Mapping[str, str]
    component_goals: Mapping[str, str]
    permission_ceiling: frozenset[str]

    def __post_init__(self) -> None:
        project_id = _plain_text(self.project_id, "project_id")
        if type(self.expected_spec_version) is not int or self.expected_spec_version < 1:
            raise PackagedProductFactoryPreparationError(
                "expected_spec_version must be a positive integer"
            )
        if type(self.expected_row_version) is not int or self.expected_row_version < 0:
            raise PackagedProductFactoryPreparationError(
                "expected_row_version must be a non-negative integer"
            )
        if type(self.graph_version) is not int or self.graph_version < 1:
            raise PackagedProductFactoryPreparationError(
                "graph_version must be a positive integer"
            )
        graph = _snapshot_graph(self.graph)
        if graph.project_id != project_id:
            raise PackagedProductFactoryPreparationError(
                "repository graph does not belong to the execution-plan ProductProject"
            )

        base_shas = _text_mapping(self.base_shas, "base_shas")
        repository_ids = {item.repository_id for item in graph.repositories}
        if set(base_shas) != repository_ids:
            raise PackagedProductFactoryPreparationError(
                "base_shas must cover exactly the repository graph"
            )
        for repository_id, sha in tuple(base_shas.items()):
            if _SHA_RE.fullmatch(sha) is None:
                raise PackagedProductFactoryPreparationError(
                    f"base SHA is invalid for repository {repository_id}"
                )
            base_shas[repository_id] = sha.casefold()

        component_goals = _text_mapping(self.component_goals, "component_goals")
        component_ids = {item.component_id for item in graph.components}
        if set(component_goals) != component_ids:
            raise PackagedProductFactoryPreparationError(
                "component_goals must cover exactly the repository graph components"
            )

        if type(self.permission_ceiling) is not frozenset or not self.permission_ceiling:
            raise PackagedProductFactoryPreparationError(
                "permission_ceiling must be a non-empty frozenset"
            )
        permissions = frozenset(
            _plain_text(permission, "permission_ceiling item")
            for permission in self.permission_ceiling
        )

        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "graph", graph)
        object.__setattr__(self, "base_shas", MappingProxyType(base_shas))
        object.__setattr__(
            self,
            "component_goals",
            MappingProxyType(component_goals),
        )
        object.__setattr__(self, "permission_ceiling", permissions)


@dataclass(frozen=True, slots=True)
class PreparedProductFactory:
    host_task_id: str
    state: MultiRepositoryExecutionState

    @property
    def project_id(self) -> str:
        return self.state.authority.project_id

    @property
    def spec_version(self) -> int:
        return self.state.authority.spec_version

    @property
    def row_version(self) -> int:
        return self.state.authority.row_version

    @property
    def graph_digest(self) -> str:
        return self.state.authority.graph_digest


def product_factory_host_task_identity(
    project_id: str,
    *,
    spec_version: int,
    row_version: int,
) -> str:
    """Derive one deterministic host task for one exact ProductProject version."""

    project_id = _plain_text(project_id, "project_id")
    if type(spec_version) is not int or spec_version < 1:
        raise PackagedProductFactoryPreparationError(
            "spec_version must be a positive integer"
        )
    if type(row_version) is not int or row_version < 0:
        raise PackagedProductFactoryPreparationError(
            "row_version must be a non-negative integer"
        )
    identity = (
        "urn:nika:product-factory-host:v1:"
        f"{project_id}:{spec_version}:{row_version}"
    )
    return str(uuid5(NAMESPACE_URL, identity))


class PackagedProductFactoryPreparationService:
    """Bind trusted execution inputs to the incumbent durable Product Factory host.

    This service never infers a repository, component, base SHA or permission from
    user text. Those values must already be authoritative and are snapshotted by
    PackagedProductFactoryExecutionPlan before this boundary is crossed.
    """

    def __init__(
        self,
        *,
        repository: ProductProjectRepository,
        tasks: TaskQueue,
        host: MultiRepositoryProductFactoryHost,
        workspace_id: str,
    ) -> None:
        self._repository = repository
        self._tasks = tasks
        self._host = host
        self._workspace_id = _plain_text(workspace_id, "workspace_id")
        stores = {
            _store_identity(repository.store),
            _store_identity(tasks.store),
            _store_identity(host.store),
        }
        if len(stores) != 1:
            raise PackagedProductFactoryPreparationError(
                "ProductProject, task and Product Factory authorities must share one database"
            )

    def prepare(
        self,
        plan: PackagedProductFactoryExecutionPlan,
    ) -> PreparedProductFactory:
        plan = _snapshot_execution_plan(plan)
        project = self._repository.get(plan.project_id)
        self._require_plan_version(project, plan)
        task_id = product_factory_host_task_identity(
            project.project_id,
            spec_version=project.spec_version,
            row_version=project.row_version,
        )
        self._ensure_host_task(project, task_id=task_id, create=True)
        state = self._host.initialize(
            host_task_id=task_id,
            project=project,
            graph=plan.graph,
            graph_version=plan.graph_version,
            base_shas=dict(plan.base_shas),
            component_goals=dict(plan.component_goals),
            permission_ceiling=plan.permission_ceiling,
        )
        return PreparedProductFactory(host_task_id=task_id, state=state)

    def restore(self, project_id: str) -> PreparedProductFactory:
        project = self._repository.get(_plain_text(project_id, "project_id"))
        task_id = product_factory_host_task_identity(
            project.project_id,
            spec_version=project.spec_version,
            row_version=project.row_version,
        )
        self._ensure_host_task(project, task_id=task_id, create=False)
        state = self._host.restore(host_task_id=task_id, project=project)
        return PreparedProductFactory(host_task_id=task_id, state=state)

    def require_repair_request(
        self,
        project_id: str,
        component_id: str,
    ) -> tuple[str, ComponentWorkRequest]:
        """Return only an exact durable REPAIR_REQUIRED request for later Toolsmith use."""

        component_id = _plain_text(component_id, "component_id")
        prepared = self.restore(project_id)
        matches = tuple(
            record
            for record in prepared.state.coordinator.snapshot().records
            if record.request.component_id == component_id
        )
        if len(matches) != 1:
            raise PackagedProductFactoryPreparationError(
                "component is not uniquely present in the durable Product Factory plan"
            )
        record = matches[0]
        if record.state is not WorkState.REPAIR_REQUIRED or record.result is None:
            raise PackagedProductFactoryPreparationError(
                "component is not an exact durable REPAIR_REQUIRED work item"
            )
        if record.result.failure is None:
            raise PackagedProductFactoryPreparationError(
                "repair-required component lacks worker-failure evidence"
            )
        return prepared.host_task_id, record.request

    def preview_repair(
        self,
        prepared: PreparedProductFactory,
        *,
        component_id: str,
        reason: str,
    ) -> ComponentWorkRequest:
        """Preview one exact repair without changing coordinator or durable state."""

        if type(prepared) is not PreparedProductFactory:
            raise TypeError("prepared must be PreparedProductFactory")
        return self._host.preview_repair(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            component_id=_plain_text(component_id, "component_id"),
            reason=_plain_text(reason, "reason"),
        )

    def commit_repair(
        self,
        prepared: PreparedProductFactory,
        *,
        component_id: str,
        reason: str,
        expected_next_work_id: str,
    ) -> ComponentWorkRequest:
        """Persist lineage and checkpoint only for the exact prior preview identity."""

        if type(prepared) is not PreparedProductFactory:
            raise TypeError("prepared must be PreparedProductFactory")
        request, _intent = self._host.commit_repair_and_checkpoint(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            component_id=_plain_text(component_id, "component_id"),
            reason=_plain_text(reason, "reason"),
            expected_next_work_id=_plain_text(
                expected_next_work_id,
                "expected_next_work_id",
            ),
        )
        return request

    @staticmethod
    def _require_plan_version(
        project: ProductProject,
        plan: PackagedProductFactoryExecutionPlan,
    ) -> None:
        if (
            project.spec_version != plan.expected_spec_version
            or project.row_version != plan.expected_row_version
        ):
            raise PackagedProductFactoryPreparationError(
                "trusted execution plan is stale for the current ProductProject"
            )

    def _ensure_host_task(
        self,
        project: ProductProject,
        *,
        task_id: str,
        create: bool,
    ) -> None:
        base_payload = {
            "kind": _HOST_TASK_KIND,
            "product_project_id": project.project_id,
        }
        try:
            task = self._tasks.get(task_id)
        except KeyError:
            if not create:
                raise PackagedProductFactoryPreparationError(
                    "Product Factory host task is not prepared for the current ProductProject"
                ) from None
            task = self._tasks.create_exact(
                task_id=task_id,
                workspace_id=self._workspace_id,
                agent_id=PRODUCT_FACTORY_HOST_AGENT_ID,
                payload=base_payload,
            )
        except TaskPayloadCorruptionError as exc:
            raise PackagedProductFactoryPreparationError(
                "Product Factory host task payload is corrupt"
            ) from exc

        if (
            task.workspace_id != self._workspace_id
            or task.agent_id != PRODUCT_FACTORY_HOST_AGENT_ID
            or task.payload.get("kind") != _HOST_TASK_KIND
            or task.payload.get("product_project_id") != project.project_id
        ):
            raise PackagedProductFactoryPreparationError(
                "deterministic Product Factory host task conflicts with existing authority"
            )


def _snapshot_execution_plan(
    plan: object,
) -> PackagedProductFactoryExecutionPlan:
    """Re-admit current fields before any trusted Product Factory preparation effect."""

    if type(plan) is not PackagedProductFactoryExecutionPlan:
        raise TypeError("plan must be PackagedProductFactoryExecutionPlan")
    try:
        return PackagedProductFactoryExecutionPlan(
            project_id=plan.project_id,
            expected_spec_version=plan.expected_spec_version,
            expected_row_version=plan.expected_row_version,
            graph=plan.graph,
            graph_version=plan.graph_version,
            base_shas=plan.base_shas,
            component_goals=plan.component_goals,
            permission_ceiling=plan.permission_ceiling,
        )
    except (AttributeError, TypeError) as exc:
        raise PackagedProductFactoryPreparationError(
            "execution plan is structurally invalid"
        ) from exc


def _snapshot_graph(graph: ProductRepositoryGraph) -> ProductRepositoryGraph:
    if type(graph) is not ProductRepositoryGraph:
        raise TypeError("graph must be ProductRepositoryGraph")
    try:
        project_id = graph.project_id
        repositories_value = graph.repositories
        components_value = graph.components
    except AttributeError as exc:
        raise PackagedProductFactoryPreparationError(
            "repository graph is structurally invalid"
        ) from exc
    if type(project_id) is not str:
        raise PackagedProductFactoryPreparationError(
            "repository graph project_id must be text"
        )
    if type(repositories_value) is not tuple or type(components_value) is not tuple:
        raise PackagedProductFactoryPreparationError(
            "repository graph collections must be exact tuples"
        )
    repositories = tuple(
        _snapshot_repository_ref(item) for item in repositories_value
    )
    components = tuple(
        _snapshot_product_component(item) for item in components_value
    )
    try:
        return ProductRepositoryGraph(
            project_id=project_id,
            repositories=repositories,
            components=components,
        )
    except (AttributeError, TypeError, RepositoryGraphError) as exc:
        raise PackagedProductFactoryPreparationError(
            "repository graph is structurally invalid"
        ) from exc


def _snapshot_repository_ref(value: object) -> RepositoryRef:
    if type(value) is not RepositoryRef:
        raise PackagedProductFactoryPreparationError(
            "repository graph entries must be exact RepositoryRef values"
        )
    try:
        repository_id = value.repository_id
        provider = value.provider
        locator = value.locator
        default_branch = value.default_branch
        credential_ref = value.credential_ref
        case_sensitive_paths = value.case_sensitive_paths
    except AttributeError as exc:
        raise PackagedProductFactoryPreparationError(
            "repository graph repository entry is structurally invalid"
        ) from exc
    if any(
        type(item) is not str
        for item in (repository_id, provider, locator, default_branch)
    ):
        raise PackagedProductFactoryPreparationError(
            "repository graph repository identity fields must be text"
        )
    if credential_ref is not None and type(credential_ref) is not str:
        raise PackagedProductFactoryPreparationError(
            "repository graph credential_ref must be text or None"
        )
    if type(case_sensitive_paths) is not bool:
        raise PackagedProductFactoryPreparationError(
            "repository graph case_sensitive_paths must be bool"
        )
    try:
        return RepositoryRef(
            repository_id=repository_id,
            provider=provider,
            locator=locator,
            default_branch=default_branch,
            credential_ref=credential_ref,
            case_sensitive_paths=case_sensitive_paths,
        )
    except (AttributeError, TypeError, RepositoryGraphError) as exc:
        raise PackagedProductFactoryPreparationError(
            "repository graph repository entry is invalid"
        ) from exc


def _snapshot_product_component(value: object) -> ProductComponent:
    if type(value) is not ProductComponent:
        raise PackagedProductFactoryPreparationError(
            "repository graph components must be exact ProductComponent values"
        )
    try:
        component_id = value.component_id
        repository_id = value.repository_id
        paths = value.paths
        dependencies = value.dependencies
        build_commands = value.build_commands
        test_commands = value.test_commands
        release_identity = value.release_identity
    except AttributeError as exc:
        raise PackagedProductFactoryPreparationError(
            "repository graph component is structurally invalid"
        ) from exc
    if type(component_id) is not str or type(repository_id) is not str:
        raise PackagedProductFactoryPreparationError(
            "repository graph component identity fields must be text"
        )
    paths_snapshot = _exact_text_tuple(paths, "component paths")
    dependencies_snapshot = _exact_text_tuple(
        dependencies,
        "component dependencies",
    )
    build_snapshot = _exact_command_tuple(
        build_commands,
        "component build_commands",
    )
    test_snapshot = _exact_command_tuple(
        test_commands,
        "component test_commands",
    )
    if release_identity is not None and type(release_identity) is not str:
        raise PackagedProductFactoryPreparationError(
            "repository graph release_identity must be text or None"
        )
    try:
        return ProductComponent(
            component_id=component_id,
            repository_id=repository_id,
            paths=paths_snapshot,
            dependencies=dependencies_snapshot,
            build_commands=build_snapshot,
            test_commands=test_snapshot,
            release_identity=release_identity,
        )
    except (AttributeError, TypeError, RepositoryGraphError) as exc:
        raise PackagedProductFactoryPreparationError(
            "repository graph component is invalid"
        ) from exc


def _exact_text_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise PackagedProductFactoryPreparationError(
            f"{label} must be an exact tuple of text"
        )
    return value


def _exact_command_tuple(
    value: object,
    label: str,
) -> tuple[tuple[str, ...], ...]:
    if type(value) is not tuple:
        raise PackagedProductFactoryPreparationError(
            f"{label} must be an exact tuple of argv tuples"
        )
    for command in value:
        if type(command) is not tuple or any(
            type(part) is not str for part in command
        ):
            raise PackagedProductFactoryPreparationError(
                f"{label} must be an exact tuple of argv tuples"
            )
    return value


def _text_mapping(value: Mapping[str, str], label: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PackagedProductFactoryPreparationError(f"{label} must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = _plain_text(key, f"{label} key")
        item_text = _plain_text(item, f"{label} value")
        if key_text in result:
            raise PackagedProductFactoryPreparationError(
                f"{label} contains duplicate normalized keys"
            )
        result[key_text] = item_text
    return result


def _plain_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise PackagedProductFactoryPreparationError(
            f"{label} must be normalized non-empty text"
        )
    return value


def _store_identity(store: object) -> str:
    path = getattr(store, "path", None)
    if not isinstance(path, (str, Path)):
        raise PackagedProductFactoryPreparationError(
            "SQLite authority does not expose a stable path"
        )
    return str(Path(path).resolve())
