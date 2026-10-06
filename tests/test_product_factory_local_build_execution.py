from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from dataclasses import dataclass

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionPortError,
    BuildExecutionScopeRequest,
    BuildExecutionSpec,
    BuildExecutionState,
    ExecutionGrant,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import (
    BuildOutputPolicy,
    DurableBuildExecutionHost,
    SQLiteBuildExecutionCheckpointStore,
)
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_deployment import (
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)
from nika_core.product_factory_local_build_execution import (
    PackagedLocalBuildExecutionNode,
    _dispatch_digest,
)
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.contracts import AllowedPathPolicy, ResourceBudget


@dataclass
class RepositoryAuthority:
    root: pathlib.Path
    drift_root: pathlib.Path | None = None
    drift_after: int | None = None
    calls: int = 0

    def resolve(self, dispatch: BuildExecutionDispatch) -> pathlib.Path:
        self.calls += 1
        if (
            self.drift_root is not None
            and self.drift_after is not None
            and self.calls > self.drift_after
        ):
            return self.drift_root
        return self.root


@dataclass
class Authority:
    value: ProjectExecutionAuthority
    calls: int = 0
    drift_after: int | None = None

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        self.calls += 1
        if self.drift_after is not None and self.calls > self.drift_after:
            current = self.value
            return ProjectExecutionAuthority(
                current.project_id,
                current.repository_id,
                current.work_id,
                current.permissions,
                current.allowed_node_ids,
                current.allowed_workspace_paths,
                current.network_scopes,
                current.credential_refs,
                current.commands,
                ("authority://drifted",),
            )
        return self.value


@dataclass
class Policies:
    value: BuildOutputPolicy

    def resolve(self, *, project_id: str, repository_id: str, work_id: str):
        return self.value


@dataclass
class Available:
    def is_available(self, node_id: str) -> bool:
        return True


@dataclass
class LostAckPort:
    inner: PackagedLocalBuildExecutionNode
    run_calls: int = 0
    inspect_calls: int = 0

    def run(self, dispatch: BuildExecutionDispatch):
        self.run_calls += 1
        self.inner.run(dispatch)
        raise BuildExecutionPortError("simulated lost acknowledgement")

    def inspect(self, dispatch: BuildExecutionDispatch):
        self.inspect_calls += 1
        return self.inner.inspect(dispatch)


def _platform() -> Platform:
    return Platform.WINDOWS if os.name == "nt" else Platform.LINUX


def _path_identity() -> RepositoryPathIdentity:
    return (
        RepositoryPathIdentity.CASE_INSENSITIVE
        if os.name == "nt"
        else RepositoryPathIdentity.CASE_SENSITIVE
    )


def _git() -> str:
    value = shutil.which("git")
    if value is None:
        pytest.skip("git is required for the production local-build adapter test")
    return str(pathlib.Path(value).resolve())


def _python() -> str:
    return str(pathlib.Path(sys.executable).resolve())


def _repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    root = tmp_path / "source repo"
    output = root / "products" / "build"
    output.mkdir(parents=True)
    (output / "input.txt").write_text("source\n", encoding="utf-8")
    git = _git()
    commands = (
        (git, "-C", str(root), "init"),
        (git, "-C", str(root), "config", "user.email", "tests@example.invalid"),
        (git, "-C", str(root), "config", "user.name", "Nika Tests"),
        (git, "-C", str(root), "add", "."),
        (git, "-C", str(root), "commit", "-m", "fixture"),
    )
    for command in commands:
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    sha = subprocess.run(
        (git, "-C", str(root), "rev-parse", "HEAD"),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return root, sha


def _command(
    *,
    secret: str | None = None,
    outside: bool = False,
    production_escape: pathlib.Path | None = None,
) -> tuple[str, ...]:
    statements = ["from pathlib import Path"]
    if secret is not None:
        statements.append(f"print({secret!r})")
    if outside:
        statements.append(
            "Path('../../outside.txt').write_text('outside', encoding='utf-8')"
        )
    if production_escape is not None:
        statements.append(
            f"Path({str(production_escape)!r}).write_text('escape', encoding='utf-8')"
        )
    statements.append("Path('artifact.bin').write_bytes(b'nika-build-artifact')")
    return (_python(), "-c", "; ".join(statements))


def _authority(
    work_id: str,
    command: tuple[str, ...],
    *,
    network_scopes: tuple[str, ...] = (),
) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        work_id,
        frozenset({"build_release"}),
        ("local-1",),
        ("products/build",),
        network_scopes,
        (),
        (ApprovedBuildCommand("build", command),),
        ("authority://trusted-plan/1",),
    )


def _dispatch(
    sha: str,
    work_id: str,
    command: tuple[str, ...],
    *,
    network_scopes: tuple[str, ...] = (),
) -> BuildExecutionDispatch:
    grant = ExecutionGrant(
        "project-1",
        "repo-main",
        work_id,
        "products/build",
        ("local-1",),
        network_scopes,
        (),
        "build",
        command,
        ("authority://trusted-plan/1",),
    )
    return BuildExecutionDispatch(
        f"dispatch:project-1:{work_id}:1",
        "project-1",
        work_id,
        "local-1",
        _platform(),
        sha,
        grant,
        1,
    )


def _policy(work_id: str) -> BuildOutputPolicy:
    return BuildOutputPolicy(
        "project-1",
        "repo-main",
        work_id,
        AllowedPathPolicy(("products/build",)),
        8,
        _path_identity(),
    )


def _startup(tmp_path: pathlib.Path) -> PackagedLocalProductFactoryStartup:
    workspace_parent = tmp_path / "workspaces"
    workspace_parent.mkdir()
    return PackagedLocalProductFactoryStartup(
        workspace_parent,
        ContainedLocalCodingPolicy(
            (_python(),),
            ResourceBudget(
                timeout_seconds=60,
                max_output_bytes=1024 * 1024,
                max_changed_files=8,
            ),
            lease_seconds=120,
        ),
        pathlib.Path(_git()),
    )


def _adapter(
    tmp_path: pathlib.Path,
    store: SQLiteStore,
    root: pathlib.Path,
    authority: Authority,
    policy: Policies,
) -> PackagedLocalBuildExecutionNode:
    return PackagedLocalBuildExecutionNode(
        store=store,
        node_id="local-1",
        startup=_startup(tmp_path),
        repositories=RepositoryAuthority(root),
        trusted_authority=authority,
        output_policies=policy,
    )


def _store(tmp_path: pathlib.Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_local_build_runs_exact_source_in_private_contained_workspace(tmp_path) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command()
    authority = Authority(_authority("work-1", command))
    adapter = _adapter(
        tmp_path,
        store,
        root,
        authority,
        Policies(_policy("work-1")),
    )
    dispatch = _dispatch(sha, "work-1", command)

    result = adapter.run(dispatch)

    assert result.succeeded is True
    assert result.uncertain is False
    assert result.source_sha == sha
    changed = adapter.collect(dispatch, result)
    assert [item.path for item in changed] == ["products/build/artifact.bin"]
    assert len(result.artifact_digest) == 64
    assert (root / "products" / "build" / "artifact.bin").exists() is False
    status = subprocess.run(
        (_git(), "-C", str(root), "status", "--porcelain"),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout
    assert status == ""
    assert adapter.inspect(dispatch) == result


def test_durable_receipt_recovers_after_process_lost_ack_without_replay(tmp_path) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command()
    authority = Authority(_authority("work-ack", command))
    policies = Policies(_policy("work-ack"))
    adapter = _adapter(tmp_path, store, root, authority, policies)
    lost_ack = LostAckPort(adapter)

    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "project-1"},
    )
    registry = ExecutionNodeRegistry()
    registry.register(
        ExecutionNode(
            NodeIdentity("local-1", _platform(), "x86_64", "instance-local-1"),
            NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
            ResourceEnvelope(8, 16384, 65536),
        )
    )
    request = ExecutionRequest(
        "project-1",
        "work-ack",
        _platform(),
        frozenset({"build"}),
        frozenset({"python"}),
        ResourceEnvelope(2, 2048, 4096),
    )
    spec = BuildExecutionSpec(
        request,
        sha,
        BuildExecutionScopeRequest(
            "repo-main",
            "products/build",
            ("local-1",),
            (),
            (),
            "build",
        ),
        120,
    )
    coordinator = BuildExecutionCoordinator(registry, Available(), authority)
    checkpoint = SQLiteBuildExecutionCheckpointStore(
        store,
        task.task_id,
        "project-1",
    )
    host = DurableBuildExecutionHost(
        coordinator,
        lost_ack,
        adapter,
        policies,
        checkpoint,
    )
    host.submit(spec)
    host.prepare("work-ack")
    host.begin_dispatch("work-ack")

    uncertain = host.execute("work-ack")

    assert uncertain.state is BuildExecutionState.RECONCILE_REQUIRED
    assert lost_ack.run_calls == 1
    assert (root / "products" / "build" / "artifact.bin").exists() is False

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.register(
        ExecutionNode(
            NodeIdentity("local-1", _platform(), "x86_64", "instance-local-1"),
            NodeCapabilities(frozenset({"build"}), frozenset({"python"}), False),
            ResourceEnvelope(8, 16384, 65536),
        )
    )
    restarted_adapter = PackagedLocalBuildExecutionNode(
        store=store,
        node_id="local-1",
        startup=adapter.startup,
        repositories=RepositoryAuthority(root),
        trusted_authority=authority,
        output_policies=policies,
    )
    restarted = DurableBuildExecutionHost(
        BuildExecutionCoordinator(restarted_registry, Available(), authority),
        restarted_adapter,
        restarted_adapter,
        policies,
        SQLiteBuildExecutionCheckpointStore(store, task.task_id, "project-1"),
    )
    restored = restarted.restore_latest()
    assert restored.coordinator.records[0].state is BuildExecutionState.RECONCILE_REQUIRED

    completed = restarted.reconcile("work-ack")

    assert completed.state is BuildExecutionState.SUCCEEDED
    latest = restarted.snapshot()
    assert latest.coordinator.records[0].evidence is not None
    assert [item.path for item in latest.file_evidence[0].changed_files] == [
        "products/build/artifact.bin"
    ]


def test_started_without_receipt_never_blindly_replays(tmp_path, monkeypatch) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command()
    adapter = _adapter(
        tmp_path,
        store,
        root,
        Authority(_authority("work-started", command)),
        Policies(_policy("work-started")),
    )
    dispatch = _dispatch(sha, "work-started", command)
    adapter._claim_effect(dispatch, _dispatch_digest(dispatch))

    def forbidden(*args, **kwargs):
        raise AssertionError("process must not be replayed")

    monkeypatch.setattr(
        "nika_core.product_factory_local_build_execution.run_typed_process",
        forbidden,
    )

    assert adapter.inspect(dispatch) is None
    with pytest.raises(BuildExecutionPortError, match="already started"):
        adapter.run(dispatch)


def test_second_run_reuses_durable_receipt_without_second_process(tmp_path, monkeypatch) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command()
    adapter = _adapter(
        tmp_path,
        store,
        root,
        Authority(_authority("work-repeat", command)),
        Policies(_policy("work-repeat")),
    )
    dispatch = _dispatch(sha, "work-repeat", command)
    first = adapter.run(dispatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("known durable result must not execute twice")

    monkeypatch.setattr(
        "nika_core.product_factory_local_build_execution.run_typed_process",
        forbidden,
    )

    assert adapter.run(dispatch) == first


def test_network_grant_is_rejected_before_effect_marker(tmp_path) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command()
    scopes = ("example.invalid:443",)
    adapter = _adapter(
        tmp_path,
        store,
        root,
        Authority(_authority("work-network", command, network_scopes=scopes)),
        Policies(_policy("work-network")),
    )
    dispatch = _dispatch(
        sha,
        "work-network",
        command,
        network_scopes=scopes,
    )

    with pytest.raises(BuildExecutionPortError, match="preparation failed"):
        adapter.run(dispatch)

    assert adapter.inspect(dispatch) is None


def test_authority_drift_after_start_fails_without_launching_process(
    tmp_path,
    monkeypatch,
) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command()
    authority = Authority(
        _authority("work-drift", command),
        drift_after=1,
    )
    adapter = _adapter(
        tmp_path,
        store,
        root,
        authority,
        Policies(_policy("work-drift")),
    )
    dispatch = _dispatch(sha, "work-drift", command)

    def forbidden(*args, **kwargs):
        raise AssertionError("process must not launch after authority drift")

    monkeypatch.setattr(
        "nika_core.product_factory_local_build_execution.run_typed_process",
        forbidden,
    )

    result = adapter.run(dispatch)

    assert result.succeeded is False
    assert result.uncertain is False
    assert adapter.inspect(dispatch) == result
    assert adapter.collect(dispatch, result) == ()


def test_repository_authority_drift_after_process_is_durable_uncertainty(
    tmp_path,
) -> None:
    root, sha = _repository(tmp_path)
    drift_root = tmp_path / "replacement-repository"
    drift_root.mkdir()
    store = _store(tmp_path)
    command = _command()
    repository_authority = RepositoryAuthority(
        root,
        drift_root=drift_root,
        drift_after=2,
    )
    authority = Authority(_authority("work-repository-drift", command))
    policies = Policies(_policy("work-repository-drift"))
    adapter = PackagedLocalBuildExecutionNode(
        store=store,
        node_id="local-1",
        startup=_startup(tmp_path),
        repositories=repository_authority,
        trusted_authority=authority,
        output_policies=policies,
    )
    dispatch = _dispatch(sha, "work-repository-drift", command)

    result = adapter.run(dispatch)

    assert result.succeeded is False
    assert result.uncertain is True
    assert repository_authority.calls == 3
    assert adapter.inspect(dispatch) == result
    with pytest.raises(BuildExecutionPortError, match="cannot publish"):
        adapter.collect(dispatch, result)


def test_out_of_policy_private_workspace_mutation_is_failed_not_published(tmp_path) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command(outside=True)
    adapter = _adapter(
        tmp_path,
        store,
        root,
        Authority(_authority("work-scope", command)),
        Policies(_policy("work-scope")),
    )
    dispatch = _dispatch(sha, "work-scope", command)

    result = adapter.run(dispatch)

    assert result.succeeded is False
    assert result.uncertain is False
    assert adapter.collect(dispatch, result) == ()
    assert (root / "outside.txt").exists() is False
    assert (root / "products" / "build" / "artifact.bin").exists() is False


def test_absolute_path_escape_into_production_repository_fails_integrity_gate(
    tmp_path,
) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    escaped = root / "escaped-by-build.txt"
    command = _command(production_escape=escaped)
    adapter = _adapter(
        tmp_path,
        store,
        root,
        Authority(_authority("work-prod-escape", command)),
        Policies(_policy("work-prod-escape")),
    )
    dispatch = _dispatch(sha, "work-prod-escape", command)

    result = adapter.run(dispatch)

    assert result.succeeded is False
    assert result.uncertain is False
    assert adapter.collect(dispatch, result) == ()
    assert escaped.read_text(encoding="utf-8") == "escape"
    assert adapter.inspect(dispatch) == result


def test_process_output_is_not_persisted_in_receipt(tmp_path) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    secret = "DO-NOT-PERSIST-THIS-OUTPUT"
    command = _command(secret=secret)
    adapter = _adapter(
        tmp_path,
        store,
        root,
        Authority(_authority("work-secret", command)),
        Policies(_policy("work-secret")),
    )
    result = adapter.run(_dispatch(sha, "work-secret", command))
    assert result.succeeded is True

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM audit_events "
            "WHERE entity_type='product_factory_build_dispatch'"
        ).fetchall()
    serialized = "\n".join(str(row["payload_json"]) for row in rows)
    assert secret not in serialized
    assert command[2] not in serialized


def test_duplicate_receipt_fails_closed_as_durable_corruption(tmp_path) -> None:
    root, sha = _repository(tmp_path)
    store = _store(tmp_path)
    command = _command()
    adapter = _adapter(
        tmp_path,
        store,
        root,
        Authority(_authority("work-corrupt", command)),
        Policies(_policy("work-corrupt")),
    )
    dispatch = _dispatch(sha, "work-corrupt", command)
    adapter.run(dispatch)
    assert adapter.audit is not None
    events = adapter.audit.list_for(
        entity_type="product_factory_build_dispatch",
        entity_id=dispatch.dispatch_id,
    )
    receipt = next(
        event
        for event in events
        if event.event_type == "product_factory.local_build.receipt"
    )
    adapter.audit.append(
        event_type="product_factory.local_build.receipt",
        entity_type="product_factory_build_dispatch",
        entity_id=dispatch.dispatch_id,
        payload=receipt.payload,
    )

    with pytest.raises(BuildExecutionPortError, match="receipt is invalid"):
        adapter.inspect(dispatch)


def test_dispatch_digest_does_not_serialize_raw_command_or_credentials() -> None:
    command = (
        _python(),
        "-c",
        "print('sensitive-command-text')",
    )
    dispatch = _dispatch("a" * 40, "work-digest", command)

    digest = _dispatch_digest(dispatch)

    assert len(digest) == 64
    assert "sensitive-command-text" not in digest
    assert json.dumps(list(command)) not in digest
