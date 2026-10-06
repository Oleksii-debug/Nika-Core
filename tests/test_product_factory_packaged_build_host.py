from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionError,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    BuildExecutionState,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import (
    BuildExecutionDurabilityError,
    BuildOutputPolicy,
)
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_deployment import (
    ExecutionNode,
    ExecutionRequest,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_orchestration import RepositoryRef
from nika_core.product_factory_packaged_build_host import (
    PackagedLocalBuildHostError,
    build_packaged_local_durable_build_host,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import AllowedPathPolicy, ResourceBudget

PROJECT_ID = "project-pf5-packaged"
REPOSITORY_ID = "repo-main"
WORK_ID = "pf5-build:" + "1" * 64
SOURCE_SHA = "a" * 40
NODE_ID = "packaged-local-1"


@dataclass
class Authority:
    value: ProjectExecutionAuthority

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.value


@dataclass
class Policies:
    value: BuildOutputPolicy

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.value


def _platform() -> Platform:
    return Platform.WINDOWS if os.name == "nt" else Platform.LINUX


def _path_identity() -> RepositoryPathIdentity:
    return (
        RepositoryPathIdentity.CASE_INSENSITIVE
        if os.name == "nt"
        else RepositoryPathIdentity.CASE_SENSITIVE
    )


def _git() -> Path:
    value = shutil.which("git")
    if value is None:
        pytest.skip("git is required for the packaged PF5 composition proof")
    return Path(value).resolve()


def _python() -> Path:
    return Path(sys.executable).resolve()


def _real_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "real source repo"
    output = root / "products" / "build"
    output.mkdir(parents=True)
    (output / "input.txt").write_text("source\n", encoding="utf-8")
    git = str(_git())
    for command in (
        (git, "-C", str(root), "init"),
        (git, "-C", str(root), "config", "user.email", "tests@example.invalid"),
        (git, "-C", str(root), "config", "user.name", "Nika Tests"),
        (git, "-C", str(root), "add", "."),
        (git, "-C", str(root), "commit", "-m", "fixture"),
    ):
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    source_sha = subprocess.run(
        (git, "-C", str(root), "rev-parse", "HEAD"),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return root, source_sha


def _real_build_command() -> tuple[str, ...]:
    return (
        str(_python()),
        "-c",
        (
            "from pathlib import Path; "
            "Path('artifact.bin').write_bytes(b'nika-packaged-build')"
        ),
    )


def _real_startup(tmp_path: Path) -> PackagedLocalProductFactoryStartup:
    workspace = tmp_path / "PF5 real workspaces"
    workspace.mkdir()
    return PackagedLocalProductFactoryStartup(
        workspace_parent=workspace.resolve(),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(str(_python()),),
            resource_budget=ResourceBudget(
                timeout_seconds=60,
                max_output_bytes=1024 * 1024,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        git_executable=_git(),
    )


def _bind_real_repository(store: SQLiteStore, root: Path) -> None:
    locator = "https://example.invalid/nika-product.git"
    repository = RepositoryRef(
        REPOSITORY_ID,
        "git",
        locator,
        "main",
        case_sensitive_paths=os.name != "nt",
    )
    ProductProjectRepository(store).create(
        project_id=PROJECT_ID,
        name="PF5 packaged composition proof",
        spec=ProductProjectSpec(
            goal="Build the exact bound source",
            desired_outcome="Produce one verified build artifact",
            repository_refs=(locator,),
        ),
        idempotency_key="create-pf5-packaged-proof",
    )
    ProductFactoryLocalRepositoryBindings(store).bind(
        project_id=PROJECT_ID,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )


def _startup(tmp_path: Path) -> PackagedLocalProductFactoryStartup:
    workspace = tmp_path / "PF5 workspaces"
    workspace.mkdir()
    git = tmp_path / ("git.exe" if os.name == "nt" else "git")
    git.write_bytes(b"test-git-placeholder")
    if os.name != "nt":
        git.chmod(0o755)
    return PackagedLocalProductFactoryStartup(
        workspace_parent=workspace.resolve(),
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(str(git.resolve()),),
            resource_budget=ResourceBudget(
                timeout_seconds=30,
                max_output_bytes=100_000,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        git_executable=git.resolve(),
    )


def _node(
    *,
    node_id: str = NODE_ID,
    platform: Platform | None = None,
    enabled: bool = True,
) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(
            node_id,
            platform or _platform(),
            "x86_64",
            f"instance-{node_id}",
        ),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(4, 8192, 32768),
        enabled=enabled,
    )


def _authority(
    *,
    node_id: str = NODE_ID,
    evidence_ref: str = "authority://pf5/packaged-local",
    command: tuple[str, ...] = ("python", "-m", "build"),
) -> Authority:
    return Authority(
        ProjectExecutionAuthority(
            PROJECT_ID,
            REPOSITORY_ID,
            WORK_ID,
            frozenset({"build_release"}),
            (node_id,),
            ("products/build",),
            (),
            (),
            (
                ApprovedBuildCommand(
                    "build",
                    command,
                ),
            ),
            (evidence_ref,),
        )
    )


def _policy(*, node_work_id: str = WORK_ID) -> Policies:
    return Policies(
        BuildOutputPolicy(
            PROJECT_ID,
            REPOSITORY_ID,
            node_work_id,
            AllowedPathPolicy(("products/build",)),
            8,
            _path_identity(),
        )
    )


def _spec(
    *,
    node_id: str = NODE_ID,
    source_sha: str = SOURCE_SHA,
) -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            PROJECT_ID,
            WORK_ID,
            _platform(),
            frozenset({"build"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 1024, 2048),
        ),
        source_sha,
        BuildExecutionScopeRequest(
            REPOSITORY_ID,
            "products/build",
            (node_id,),
            (),
            (),
            "build",
        ),
        120,
    )


def _store_and_task(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": PROJECT_ID,
        },
    )
    return store, task.task_id


def _host(
    tmp_path: Path,
    *,
    store: SQLiteStore | None = None,
    task_id: str | None = None,
    node: ExecutionNode | None = None,
    startup: PackagedLocalProductFactoryStartup | None = None,
    authority: Authority | None = None,
):
    if store is None or task_id is None:
        store, task_id = _store_and_task(tmp_path)
    chosen_node = node or _node()
    startup = startup or _startup(tmp_path)
    authority = authority or _authority(node_id=chosen_node.identity.node_id)
    policies = _policy()
    host = build_packaged_local_durable_build_host(
        store,
        host_task_id=task_id,
        project_id=PROJECT_ID,
        node=chosen_node,
        startup=startup,
        trusted_authority=authority,
        output_policies=policies,
    )
    return host, store, task_id, startup, authority, policies


def test_composition_uses_one_canonical_registry_coordinator_and_local_node(tmp_path) -> None:
    host, _store, _task_id, _startup_value, authority, policies = _host(tmp_path)

    registry = host.coordinator.nodes.snapshot()

    assert len(registry.nodes) == 1
    assert registry.nodes[0] == _node()
    assert host.coordinator.trusted_authority is authority
    assert host.output_policies is policies
    assert host.node_port is host.file_evidence_port


def test_fresh_submit_and_prepare_are_durable_before_any_node_effect(tmp_path) -> None:
    host, _store, _task_id, _startup_value, _authority_value, _policies = _host(
        tmp_path
    )

    submitted = host.submit(_spec())
    prepared = host.prepare(WORK_ID)

    assert submitted.state is BuildExecutionState.PENDING
    assert prepared.state is BuildExecutionState.PREPARED
    assert host.checkpoints.latest().snapshot.sequence == 2


def test_packaged_composition_runs_real_bound_build_and_restores_terminal_state(
    tmp_path,
) -> None:
    root, source_sha = _real_repository(tmp_path)
    store, task_id = _store_and_task(tmp_path)
    _bind_real_repository(store, root)
    startup = _real_startup(tmp_path)
    authority = _authority(command=_real_build_command())
    policies = _policy()
    host = build_packaged_local_durable_build_host(
        store,
        host_task_id=task_id,
        project_id=PROJECT_ID,
        node=_node(),
        startup=startup,
        trusted_authority=authority,
        output_policies=policies,
    )

    host.submit(_spec(source_sha=source_sha))
    prepared = host.prepare(WORK_ID)
    dispatch = host.begin_dispatch(WORK_ID)
    completed = host.execute(WORK_ID)

    assert prepared.state is BuildExecutionState.PREPARED
    assert dispatch.source_sha == source_sha
    assert completed.state is BuildExecutionState.SUCCEEDED
    assert completed.evidence is not None
    assert completed.evidence.release_sha == source_sha
    assert completed.evidence.succeeded is True
    snapshot = host.snapshot()
    assert len(snapshot.file_evidence) == 1
    assert [item.path for item in snapshot.file_evidence[0].changed_files] == [
        "products/build/artifact.bin"
    ]
    assert (root / "products" / "build" / "artifact.bin").exists() is False
    status = subprocess.run(
        (str(_git()), "-C", str(root), "status", "--porcelain"),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    assert status == ""
    terminal_sequence = host.checkpoints.latest().snapshot.sequence
    assert terminal_sequence >= 5

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = build_packaged_local_durable_build_host(
        restarted_store,
        host_task_id=task_id,
        project_id=PROJECT_ID,
        node=_node(),
        startup=startup,
        trusted_authority=authority,
        output_policies=policies,
    )
    restored = restarted.snapshot()
    assert restored.coordinator.records[0].state is BuildExecutionState.SUCCEEDED
    assert restored.file_evidence == snapshot.file_evidence
    assert restarted.execute(WORK_ID).state is BuildExecutionState.SUCCEEDED
    assert restarted.checkpoints.latest().snapshot.sequence == terminal_sequence


def test_restart_restores_existing_pf5_state_before_returning_usable_host(tmp_path) -> None:
    host, store, task_id, startup, authority, policies = _host(tmp_path)
    host.submit(_spec())
    host.prepare(WORK_ID)

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = build_packaged_local_durable_build_host(
        restarted_store,
        host_task_id=task_id,
        project_id=PROJECT_ID,
        node=_node(),
        startup=startup,
        trusted_authority=authority,
        output_policies=policies,
    )

    restored = restarted.snapshot()
    record = restored.coordinator.records[0]
    assert record.spec == _spec()
    assert record.state is BuildExecutionState.PREPARED
    assert restored.sequence >= 2


def test_restart_does_not_substitute_a_different_local_node_for_prepared_lease(
    tmp_path,
) -> None:
    host, store, task_id, startup, authority, policies = _host(tmp_path)
    host.submit(_spec())
    host.prepare(WORK_ID)
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()

    restarted = build_packaged_local_durable_build_host(
        restarted_store,
        host_task_id=task_id,
        project_id=PROJECT_ID,
        node=_node(node_id="packaged-local-2"),
        startup=startup,
        trusted_authority=authority,
        output_policies=policies,
    )

    record = restarted.snapshot().coordinator.records[0]
    assert record.state is BuildExecutionState.WAITING_FOR_NODE
    assert record.node_id is None
    assert record.lease_id is None
    assert record.dispatch is None


def test_restart_rejects_trusted_execution_authority_drift(tmp_path) -> None:
    host, store, task_id, startup, _authority_value, policies = _host(tmp_path)
    host.submit(_spec())
    host.prepare(WORK_ID)
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()

    with pytest.raises(BuildExecutionError, match="trusted host authority"):
        build_packaged_local_durable_build_host(
            restarted_store,
            host_task_id=task_id,
            project_id=PROJECT_ID,
            node=_node(),
            startup=startup,
            trusted_authority=_authority(evidence_ref="authority://pf5/drifted"),
            output_policies=policies,
        )


def test_missing_git_marks_local_node_unavailable_without_effect(tmp_path) -> None:
    host, _store, _task_id, startup, _authority_value, _policies = _host(tmp_path)
    startup.git_executable.unlink()
    host.submit(_spec())

    record = host.prepare(WORK_ID)

    assert record.state is BuildExecutionState.WAITING_FOR_NODE
    assert record.dispatch is None
    assert record.evidence is None


def test_composition_rejects_wrong_local_platform(tmp_path) -> None:
    wrong_platform = (
        Platform.LINUX if _platform() is Platform.WINDOWS else Platform.WINDOWS
    )
    store, task_id = _store_and_task(tmp_path)

    with pytest.raises(PackagedLocalBuildHostError, match="platform"):
        build_packaged_local_durable_build_host(
            store,
            host_task_id=task_id,
            project_id=PROJECT_ID,
            node=_node(platform=wrong_platform),
            startup=_startup(tmp_path),
            trusted_authority=_authority(),
            output_policies=_policy(),
        )


def test_composition_rejects_disabled_local_node(tmp_path) -> None:
    store, task_id = _store_and_task(tmp_path)

    with pytest.raises(PackagedLocalBuildHostError, match="enabled"):
        build_packaged_local_durable_build_host(
            store,
            host_task_id=task_id,
            project_id=PROJECT_ID,
            node=_node(enabled=False),
            startup=_startup(tmp_path),
            trusted_authority=_authority(),
            output_policies=_policy(),
        )


def test_checkpoint_store_rejects_task_project_substitution(tmp_path) -> None:
    store, task_id = _store_and_task(tmp_path)

    with pytest.raises(BuildExecutionDurabilityError):
        build_packaged_local_durable_build_host(
            store,
            host_task_id=task_id,
            project_id="project-other",
            node=_node(),
            startup=_startup(tmp_path),
            trusted_authority=_authority(),
            output_policies=_policy(),
        )


def test_composition_rejects_noncanonical_resource_integer_carrier(tmp_path) -> None:
    store, task_id = _store_and_task(tmp_path)
    bad_node = ExecutionNode(
        NodeIdentity(NODE_ID, _platform(), "x86_64", "instance-local"),
        NodeCapabilities(
            frozenset({"build"}),
            frozenset({"python"}),
            False,
        ),
        ResourceEnvelope(True, 8192, 32768),
    )

    with pytest.raises(PackagedLocalBuildHostError, match="cpu_cores"):
        build_packaged_local_durable_build_host(
            store,
            host_task_id=task_id,
            project_id=PROJECT_ID,
            node=bad_node,
            startup=_startup(tmp_path),
            trusted_authority=_authority(),
            output_policies=_policy(),
        )
