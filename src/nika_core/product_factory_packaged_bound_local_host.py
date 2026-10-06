from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_local_coding import ContainedLocalCodingProgram
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBinding,
    ProductFactoryLocalRepositoryBindingError,
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_multi_repository import MultiRepositoryExecutionState
from nika_core.product_factory_orchestration import ProductRepositoryGraph
from nika_core.product_factory_packaged_local_startup import (
    _PackagedLocalOllamaAuthority,
    PackagedLocalProductFactoryStartup,
    PackagedLocalProductFactoryStartupError,
    _build_repository_bound_packaged_local_product_factory_program_with_authority,
    _resolve_packaged_local_ollama_authority,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.product_project import ProductProject, ProductProjectRepository
from nika_core.v01_model_settings import V01ModelSettings


class PackagedBoundLocalProductFactoryHostError(RuntimeError):
    """Durable local repository authority changed across packaged execution."""


@dataclass(frozen=True, slots=True)
class _ProjectSnapshot:
    project_id: str
    spec_version: int
    row_version: int


@dataclass(frozen=True, slots=True)
class _BindingSnapshot:
    project_id: str
    repository_id: str
    provider: str
    locator: str
    root: Path
    binding_version: int


@dataclass(frozen=True, slots=True)
class _ProgramEntry:
    project: _ProjectSnapshot
    program: ContainedLocalCodingProgram
    bindings: Mapping[str, _BindingSnapshot]


@dataclass(frozen=True, slots=True)
class _EntryRepositoryAuthority:
    bindings: ProductFactoryLocalRepositoryBindings
    projects: ProductProjectRepository
    expected_project: _ProjectSnapshot
    expected: Mapping[str, _BindingSnapshot]

    def require_component_root(
        self,
        *,
        project_id: str,
        repository_id: str,
        root: Path,
    ) -> None:
        expected = self.expected.get(repository_id)
        if expected is None or expected.project_id != project_id:
            raise PackagedBoundLocalProductFactoryHostError(
                "component repository is outside the prepared binding snapshot"
            )
        try:
            current = self.bindings.require(project_id, repository_id)
        except (KeyError, ProductFactoryLocalRepositoryBindingError) as exc:
            raise PackagedBoundLocalProductFactoryHostError(
                "local repository binding is unavailable during contained-local execution"
            ) from exc
        actual = _binding_snapshot(current)
        if actual != expected:
            raise PackagedBoundLocalProductFactoryHostError(
                "local repository binding changed during contained-local execution"
            )
        try:
            project = self.projects.get(project_id)
        except KeyError as exc:
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject is unavailable during contained-local execution"
            ) from exc
        if (
            _project_snapshot(project) != self.expected_project
            or expected.locator not in project.spec.repository_refs
        ):
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject changed during contained-local execution"
            )
        if Path(root) != expected.root:
            raise PackagedBoundLocalProductFactoryHostError(
                "contained-local worker root differs from prepared binding authority"
            )


class PackagedBoundLocalProductFactoryHost:
    """Plan-scoped local execution host backed by durable repository bindings.

    ProductProject / ProductRepositoryGraph remain repository identity authority.
    ProductFactoryLocalRepositoryBindings is the only local filesystem authority.
    Startup configuration contributes only workspace/process/resource policy and model
    settings remain the model-route authority. Existing Product Factory hosts, workers,
    recovery/checkpoints and ModelGateway composition are reused without replacement.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        settings: V01ModelSettings,
        startup: PackagedLocalProductFactoryStartup,
        bindings: ProductFactoryLocalRepositoryBindings | None = None,
        model_authority: _PackagedLocalOllamaAuthority | None = None,
    ) -> None:
        if type(store) is not SQLiteStore:
            raise TypeError("store must be SQLiteStore")
        if type(settings) is not V01ModelSettings:
            raise TypeError("settings must be V01ModelSettings")
        if type(startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError("startup carrier is invalid")
        if model_authority is None:
            model_authority = _resolve_packaged_local_ollama_authority(store, settings)
        elif type(model_authority) is not _PackagedLocalOllamaAuthority:
            raise TypeError("model_authority carrier is invalid")
        self.store = store
        self._settings = settings
        self._startup = startup
        self._model_authority = model_authority
        self._bindings = bindings or ProductFactoryLocalRepositoryBindings(store)
        self._projects = ProductProjectRepository(store)
        self._entries: dict[str, _ProgramEntry] = {}
        self._lock = RLock()

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
        plan = PackagedProductFactoryExecutionPlan(
            project_id=project.project_id,
            expected_spec_version=project.spec_version,
            expected_row_version=project.row_version,
            graph=graph,
            graph_version=graph_version,
            base_shas=base_shas,
            component_goals=component_goals,
            permission_ceiling=permission_ceiling,
        )
        self._bindings.resolve_for_plan(plan)
        bindings = self._bindings_for_graph(project, graph)
        entry = self._entry_for(host_task_id, project, bindings)
        state = entry.program.multi_repository_host.initialize(
            host_task_id=host_task_id,
            project=project,
            graph=graph,
            graph_version=graph_version,
            base_shas=dict(base_shas),
            component_goals=dict(component_goals),
            permission_ceiling=permission_ceiling,
        )
        self._require_state_bindings(host_task_id, state)
        return state

    def restore(
        self,
        *,
        host_task_id: str,
        project: ProductProject,
    ) -> MultiRepositoryExecutionState:
        current = self._bindings_for_project(project)
        if not current:
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject has no durable local repository bindings"
            )
        temporary = self._build_entry(project, current.values())
        state = temporary.program.multi_repository_host.restore(
            host_task_id=host_task_id,
            project=project,
        )
        exact = self._bindings_for_graph(
            state.binding.project,
            state.authority.graph,
        )
        with self._lock:
            self._entries[host_task_id] = self._build_entry(
                state.binding.project,
                exact.values(),
            )
        self._require_state_bindings(host_task_id, state)
        return state

    async def recover_running(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        max_parallel: int = 4,
    ):
        entry = self._require_state_bindings(host_task_id, state)
        return await entry.program.multi_repository_host.recover_running(
            host_task_id=host_task_id,
            state=state,
            max_parallel=max_parallel,
        )

    async def dispatch_ready(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        max_parallel: int = 4,
        max_count: int = 32,
    ):
        entry = self._require_state_bindings(host_task_id, state)
        return await entry.program.multi_repository_host.dispatch_ready(
            host_task_id=host_task_id,
            state=state,
            max_parallel=max_parallel,
            max_count=max_count,
        )

    def preview_repair(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        component_id: str,
        reason: str,
    ):
        entry = self._require_state_bindings(host_task_id, state)
        return entry.program.multi_repository_host.preview_repair(
            host_task_id=host_task_id,
            state=state,
            component_id=component_id,
            reason=reason,
        )

    def commit_repair_and_checkpoint(
        self,
        *,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
        component_id: str,
        reason: str,
        expected_next_work_id: str,
    ):
        entry = self._require_state_bindings(host_task_id, state)
        return entry.program.multi_repository_host.commit_repair_and_checkpoint(
            host_task_id=host_task_id,
            state=state,
            component_id=component_id,
            reason=reason,
            expected_next_work_id=expected_next_work_id,
        )

    def _require_model_authority_current(self) -> None:
        try:
            current = _resolve_packaged_local_ollama_authority(
                self.store,
                self._settings,
            )
        except PackagedLocalProductFactoryStartupError as exc:
            raise PackagedBoundLocalProductFactoryHostError(
                "model authority is unavailable after packaged startup; restart required"
            ) from exc
        if current != self._model_authority:
            raise PackagedBoundLocalProductFactoryHostError(
                "model authority changed after packaged startup; restart required"
            )

    def _bindings_for_project(
        self,
        project: ProductProject,
    ) -> dict[str, ProductFactoryLocalRepositoryBinding]:
        current = self._projects.get(project.project_id)
        if (
            current.spec_version != project.spec_version
            or current.row_version != project.row_version
            or current.status != "active"
        ):
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject changed before local repository binding resolution"
            )
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT repository_id FROM product_factory_local_repository_bindings "
                "WHERE project_id=? ORDER BY repository_id",
                (project.project_id,),
            ).fetchall()
        result: dict[str, ProductFactoryLocalRepositoryBinding] = {}
        for row in rows:
            repository_id = row["repository_id"]
            if type(repository_id) is not str:
                raise PackagedBoundLocalProductFactoryHostError(
                    "persisted repository binding identity is invalid"
                )
            binding = self._bindings.require(
                project.project_id,
                repository_id,
            )
            if binding.locator not in current.spec.repository_refs:
                raise PackagedBoundLocalProductFactoryHostError(
                    "durable local repository binding is outside current ProductProject"
                )
            result[repository_id] = binding
        current_after = self._projects.get(project.project_id)
        if (
            current_after.spec_version != current.spec_version
            or current_after.row_version != current.row_version
            or current_after.status != "active"
        ):
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject changed during local repository binding resolution"
            )
        return result

    def _bindings_for_graph(
        self,
        project: ProductProject,
        graph: ProductRepositoryGraph,
    ) -> dict[str, ProductFactoryLocalRepositoryBinding]:
        current = self._projects.get(project.project_id)
        if (
            current.spec_version != project.spec_version
            or current.row_version != project.row_version
            or current.status != "active"
            or graph.project_id != project.project_id
        ):
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject or repository graph changed before local execution"
            )
        result: dict[str, ProductFactoryLocalRepositoryBinding] = {}
        for repository in graph.repositories:
            binding = self._bindings.require(
                project.project_id,
                repository.repository_id,
            )
            if (
                binding.provider != repository.provider
                or binding.locator != repository.locator
                or binding.locator not in current.spec.repository_refs
            ):
                raise PackagedBoundLocalProductFactoryHostError(
                    "durable local repository binding does not match repository graph"
                )
            result[repository.repository_id] = binding
        current_after = self._projects.get(project.project_id)
        if (
            current_after.spec_version != current.spec_version
            or current_after.row_version != current.row_version
            or current_after.status != "active"
        ):
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject changed during local repository binding resolution"
            )
        return result

    def _entry_for(
        self,
        host_task_id: str,
        project: ProductProject,
        bindings: Mapping[str, ProductFactoryLocalRepositoryBinding],
    ) -> _ProgramEntry:
        expected_project = _project_snapshot(project)
        expected = _snapshot_map(bindings.values())
        with self._lock:
            current = self._entries.get(host_task_id)
            if current is not None:
                if (
                    current.project != expected_project
                    or dict(current.bindings) != expected
                ):
                    raise PackagedBoundLocalProductFactoryHostError(
                        "ProductProject or local repository bindings changed after host composition"
                    )
                return current
            entry = self._build_entry(project, bindings.values())
            self._entries[host_task_id] = entry
            return entry

    def _build_entry(
        self,
        project: ProductProject,
        bindings: Iterable[ProductFactoryLocalRepositoryBinding],
    ) -> _ProgramEntry:
        project_snapshot = _project_snapshot(project)
        snapshots = _snapshot_map(bindings)
        if not snapshots:
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject has no durable local repository bindings"
            )
        if any(
            snapshot.project_id != project_snapshot.project_id
            for snapshot in snapshots.values()
        ):
            raise PackagedBoundLocalProductFactoryHostError(
                "local repository bindings belong to another ProductProject"
            )
        repositories = {
            repository_id: snapshot.root
            for repository_id, snapshot in snapshots.items()
        }
        self._require_model_authority_current()
        program = _build_repository_bound_packaged_local_product_factory_program_with_authority(
            self.store,
            settings=self._settings,
            startup=self._startup,
            repositories=repositories,
            model_authority=self._model_authority,
        )
        program.ports.repository_authority = _EntryRepositoryAuthority(
            bindings=self._bindings,
            projects=self._projects,
            expected_project=project_snapshot,
            expected=snapshots,
        )
        return _ProgramEntry(
            project=project_snapshot,
            program=program,
            bindings=snapshots,
        )

    def _require_state_bindings(
        self,
        host_task_id: str,
        state: MultiRepositoryExecutionState,
    ) -> _ProgramEntry:
        if type(state) is not MultiRepositoryExecutionState:
            raise TypeError("state must be MultiRepositoryExecutionState")
        current = self._bindings_for_graph(
            state.binding.project,
            state.authority.graph,
        )
        expected = _snapshot_map(current.values())
        with self._lock:
            entry = self._entries.get(host_task_id)
        if entry is None:
            entry = self._entry_for(
                host_task_id,
                state.binding.project,
                current,
            )
        if entry.project != _project_snapshot(state.binding.project):
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject changed after Product Factory preparation"
            )
        selected = {
            repository_id: entry.bindings.get(repository_id)
            for repository_id in expected
        }
        if selected != expected:
            raise PackagedBoundLocalProductFactoryHostError(
                "local repository bindings changed after Product Factory preparation"
            )
        return entry


def _project_snapshot(project: ProductProject) -> _ProjectSnapshot:
    if type(project) is not ProductProject:
        raise TypeError("project must be an exact ProductProject")
    if project.status != "active":
        raise PackagedBoundLocalProductFactoryHostError(
            "ProductProject must remain active for contained-local execution"
        )
    return _ProjectSnapshot(
        project_id=project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )


def _snapshot_map(
    bindings: Iterable[ProductFactoryLocalRepositoryBinding],
) -> dict[str, _BindingSnapshot]:
    result: dict[str, _BindingSnapshot] = {}
    for binding in bindings:
        if type(binding) is not ProductFactoryLocalRepositoryBinding:
            raise TypeError("binding carrier is invalid")
        if binding.repository_id in result:
            raise PackagedBoundLocalProductFactoryHostError(
                "duplicate local repository binding identity"
            )
        result[binding.repository_id] = _binding_snapshot(binding)
    return result


def _binding_snapshot(
    binding: ProductFactoryLocalRepositoryBinding,
) -> _BindingSnapshot:
    return _BindingSnapshot(
        project_id=binding.project_id,
        repository_id=binding.repository_id,
        provider=binding.provider,
        locator=binding.locator,
        root=binding.root,
        binding_version=binding.binding_version,
    )
