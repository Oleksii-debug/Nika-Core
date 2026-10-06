from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskPayloadCorruptionError, decode_task_payload
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
from nika_core.v01_model_settings import ModelSelection, V01ModelSettings

_MODEL_AUTHORITY_KEY = "packaged_local_model_authority"
_MODEL_AUTHORITY_SCHEMA = "nika.product-factory.packaged-model-authority.v1"
_HOST_TASK_KIND = "product_factory"


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
    model_authority: _PackagedLocalOllamaAuthority


@dataclass(frozen=True, slots=True)
class _EntryRepositoryAuthority:
    bindings: ProductFactoryLocalRepositoryBindings
    projects: ProductProjectRepository
    expected_project: _ProjectSnapshot
    expected: Mapping[str, _BindingSnapshot]

    def require_repository_root(
        self,
        *,
        repository_id: str,
        root: Path,
    ) -> None:
        expected = self.expected.get(repository_id)
        if expected is None:
            raise PackagedBoundLocalProductFactoryHostError(
                "repository is outside the prepared binding snapshot"
            )
        self.require_component_root(
            project_id=expected.project_id,
            repository_id=repository_id,
            root=root,
        )

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
        model_authority, bound_now = self._bind_or_load_host_model_authority(
            host_task_id=host_task_id,
            project_id=project.project_id,
            bind_if_missing=True,
        )
        bindings = self._bindings_for_graph(project, graph)
        entry = self._entry_for(
            host_task_id,
            project,
            bindings,
            model_authority=model_authority,
            require_model_authority_current=bound_now,
        )
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
        model_authority, _ = self._bind_or_load_host_model_authority(
            host_task_id=host_task_id,
            project_id=project.project_id,
            bind_if_missing=False,
        )
        temporary = self._build_entry(
            project,
            current.values(),
            model_authority=model_authority,
            require_model_authority_current=False,
        )
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
                model_authority=model_authority,
                require_model_authority_current=False,
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

    def _bind_or_load_host_model_authority(
        self,
        *,
        host_task_id: str,
        project_id: str,
        bind_if_missing: bool,
    ) -> tuple[_PackagedLocalOllamaAuthority, bool]:
        if (
            type(host_task_id) is not str
            or not host_task_id
            or host_task_id != host_task_id.strip()
        ):
            raise PackagedBoundLocalProductFactoryHostError(
                "Product Factory host task identity is invalid"
            )
        if type(project_id) is not str or not project_id:
            raise PackagedBoundLocalProductFactoryHostError(
                "ProductProject identity is invalid"
            )

        if bind_if_missing:
            self._require_model_authority_current()

        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE" if bind_if_missing else "BEGIN")
            row = conn.execute(
                "SELECT payload_json FROM tasks WHERE task_id = ?",
                (host_task_id,),
            ).fetchone()
            if row is None:
                raise PackagedBoundLocalProductFactoryHostError(
                    "Product Factory host task does not exist"
                )
            raw_payload = row["payload_json"]
            try:
                payload = decode_task_payload(raw_payload)
            except TaskPayloadCorruptionError as exc:
                raise PackagedBoundLocalProductFactoryHostError(
                    "Product Factory host task payload is corrupt"
                ) from exc
            if (
                payload.get("kind") != _HOST_TASK_KIND
                or payload.get("product_project_id") != project_id
            ):
                raise PackagedBoundLocalProductFactoryHostError(
                    "host task is not bound to the expected ProductProject"
                )

            stored = payload.get(_MODEL_AUTHORITY_KEY)
            if stored is not None:
                return _decode_model_authority(stored), False
            if not bind_if_missing:
                raise PackagedBoundLocalProductFactoryHostError(
                    "Product Factory host task has no durable model authority"
                )

            prior_checkpoint = conn.execute(
                "SELECT 1 FROM checkpoints WHERE task_id = ? LIMIT 1",
                (host_task_id,),
            ).fetchone()
            if prior_checkpoint is not None:
                raise PackagedBoundLocalProductFactoryHostError(
                    "legacy Product Factory host task has durable state but no model authority"
                )

            authority = self._model_authority
            updated_payload = dict(payload)
            updated_payload[_MODEL_AUTHORITY_KEY] = _encode_model_authority(authority)
            canonical = _canonical_task_payload(updated_payload)
            updated = conn.execute(
                "UPDATE tasks SET payload_json = ? "
                "WHERE task_id = ? AND payload_json = ?",
                (canonical, host_task_id, raw_payload),
            )
            if updated.rowcount != 1:
                raise PackagedBoundLocalProductFactoryHostError(
                    "Product Factory host task changed while binding model authority"
                )
            conn.execute(
                "INSERT INTO audit_events("
                "event_type,entity_type,entity_id,payload_json,created_at"
                ") VALUES (?,?,?,?,?)",
                (
                    "product_factory.packaged_model_authority_bound",
                    "task",
                    host_task_id,
                    _canonical_task_payload(
                        {
                            "revision": authority.revision,
                            "selection_sha256": authority.selection_sha256,
                            "artifact_pin_sha256": authority.artifact_pin_sha256,
                        }
                    ),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return authority, True

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
        *,
        model_authority: _PackagedLocalOllamaAuthority | None = None,
        require_model_authority_current: bool = True,
    ) -> _ProgramEntry:
        expected_project = _project_snapshot(project)
        expected = _snapshot_map(bindings.values())
        expected_model_authority = (
            self._model_authority
            if model_authority is None
            else model_authority
        )
        if type(expected_model_authority) is not _PackagedLocalOllamaAuthority:
            raise TypeError("model_authority carrier is invalid")
        with self._lock:
            current = self._entries.get(host_task_id)
            if current is not None:
                if (
                    current.project != expected_project
                    or dict(current.bindings) != expected
                    or current.model_authority != expected_model_authority
                ):
                    raise PackagedBoundLocalProductFactoryHostError(
                        "ProductProject, repository bindings or model authority "
                        "changed after host composition"
                    )
                return current
            entry = self._build_entry(
                project,
                bindings.values(),
                model_authority=expected_model_authority,
                require_model_authority_current=require_model_authority_current,
            )
            self._entries[host_task_id] = entry
            return entry

    def _build_entry(
        self,
        project: ProductProject,
        bindings: Iterable[ProductFactoryLocalRepositoryBinding],
        *,
        model_authority: _PackagedLocalOllamaAuthority | None = None,
        require_model_authority_current: bool = True,
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
        selected_model_authority = (
            self._model_authority
            if model_authority is None
            else model_authority
        )
        if type(selected_model_authority) is not _PackagedLocalOllamaAuthority:
            raise TypeError("model_authority carrier is invalid")
        if require_model_authority_current:
            if selected_model_authority != self._model_authority:
                raise PackagedBoundLocalProductFactoryHostError(
                    "new Product Factory work cannot switch packaged model authority"
                )
            self._require_model_authority_current()
        program = _build_repository_bound_packaged_local_product_factory_program_with_authority(
            self.store,
            settings=self._settings,
            startup=self._startup,
            repositories=repositories,
            model_authority=selected_model_authority,
        )
        authority = _EntryRepositoryAuthority(
            bindings=self._bindings,
            projects=self._projects,
            expected_project=project_snapshot,
            expected=snapshots,
        )
        program.ports.repository_authority = authority
        program.worker.repository_authority = authority
        return _ProgramEntry(
            project=project_snapshot,
            program=program,
            bindings=snapshots,
            model_authority=selected_model_authority,
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
            model_authority, _ = self._bind_or_load_host_model_authority(
                host_task_id=host_task_id,
                project_id=state.binding.project.project_id,
                bind_if_missing=False,
            )
            entry = self._entry_for(
                host_task_id,
                state.binding.project,
                current,
                model_authority=model_authority,
                require_model_authority_current=False,
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


def _canonical_task_payload(payload: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PackagedBoundLocalProductFactoryHostError(
            "Product Factory host task payload is not canonical JSON"
        ) from exc


def _encode_model_authority(
    authority: _PackagedLocalOllamaAuthority,
) -> dict[str, object]:
    if type(authority) is not _PackagedLocalOllamaAuthority:
        raise TypeError("model_authority carrier is invalid")
    return {
        "schema": _MODEL_AUTHORITY_SCHEMA,
        "revision": authority.revision,
        "selection_sha256": authority.selection_sha256,
        "artifact_pin_sha256": authority.artifact_pin_sha256,
        "model": authority.model,
        "base_url": authority.base_url,
        "private_data_allowed": authority.private_data_allowed,
        "timeout_seconds": authority.timeout_seconds,
        "expected_manifest_sha256": authority.expected_manifest_sha256,
    }
    payload["authority_sha256"] = hashlib.sha256(
        _canonical_task_payload(payload).encode("utf-8")
    ).hexdigest()
    return payload


def _decode_model_authority(value: object) -> _PackagedLocalOllamaAuthority:
    expected = {
        "schema",
        "revision",
        "selection_sha256",
        "artifact_pin_sha256",
        "model",
        "base_url",
        "private_data_allowed",
        "timeout_seconds",
        "expected_manifest_sha256",
        "authority_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema") != _MODEL_AUTHORITY_SCHEMA
    ):
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory model authority schema is invalid"
        )

    authority_sha256 = _authority_digest(
        value["authority_sha256"],
        "authority_sha256",
        allow_none=False,
    )
    authority_payload = dict(value)
    authority_payload.pop("authority_sha256")
    expected_authority_sha256 = hashlib.sha256(
        _canonical_task_payload(authority_payload).encode("utf-8")
    ).hexdigest()
    if authority_sha256 != expected_authority_sha256:
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory model authority checksum mismatch"
        )

    revision = value["revision"]
    if type(revision) is not int or revision < 1:
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory model revision is invalid"
        )
    selection_sha256 = _authority_digest(
        value["selection_sha256"],
        "selection_sha256",
        allow_none=False,
    )
    artifact_pin_sha256 = _authority_digest(
        value["artifact_pin_sha256"],
        "artifact_pin_sha256",
        allow_none=True,
    )
    expected_manifest_sha256 = _authority_digest(
        value["expected_manifest_sha256"],
        "expected_manifest_sha256",
        allow_none=True,
    )
    private_data_allowed = value["private_data_allowed"]
    if type(private_data_allowed) is not bool:
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory private-data authority is invalid"
        )
    timeout = value["timeout_seconds"]
    if (
        type(timeout) not in (int, float)
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory model timeout is invalid"
        )
    try:
        selection = ModelSelection(
            schema_version=1,
            route_kind="ollama",
            provider_id="ollama",
            model=value["model"],
            base_url=value["base_url"],
            credential_ref=None,
            private_data_allowed=private_data_allowed,
            timeout_seconds=float(timeout),
        )
    except (TypeError, ValueError) as exc:
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory Ollama route is invalid"
        ) from exc
    recalculated_selection_sha256 = hashlib.sha256(
        selection.canonical_json().encode("utf-8")
    ).hexdigest()
    if recalculated_selection_sha256 != selection_sha256:
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory model selection digest mismatch"
        )
    if selection.model is None or selection.base_url is None:
        raise PackagedBoundLocalProductFactoryHostError(
            "durable Product Factory Ollama route is incomplete"
        )
    return _PackagedLocalOllamaAuthority(
        revision=revision,
        selection_sha256=selection_sha256,
        artifact_pin_sha256=artifact_pin_sha256,
        model=selection.model,
        base_url=selection.base_url,
        private_data_allowed=selection.private_data_allowed,
        timeout_seconds=selection.timeout_seconds,
        expected_manifest_sha256=expected_manifest_sha256,
    )


def _authority_digest(
    value: object,
    label: str,
    *,
    allow_none: bool,
) -> str | None:
    if value is None and allow_none:
        return None
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PackagedBoundLocalProductFactoryHostError(
            f"durable Product Factory {label} is invalid"
        )
    return value


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
