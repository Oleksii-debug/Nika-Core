from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_build_execution import (
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionError,
    BuildExecutionNodePort,
    BuildExecutionResult,
    BuildExecutionSpec,
    BuildExecutionState,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_deployment import (
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ResourceEnvelope,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
SHA = "a" * 40
DIGEST = "1" * 64


@dataclass
class FakeAvailability:
    unavailable: set[str]

    def is_available(self, node_id: str) -> bool:
        return node_id not in self.unavailable


@dataclass
class FakePort(BuildExecutionNodePort):
    run_result: BuildExecutionResult | None = None
    inspect_result: BuildExecutionResult | None = None
    raise_on_run: bool = False
    run_calls: int = 0
    inspect_calls: int = 0

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.run_calls += 1
        if self.raise_on_run:
            raise RuntimeError("simulated transport loss")
        if self.run_result is None:
            raise AssertionError("missing fake run result")
        return self.run_result

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        self.inspect_calls += 1
        return self.inspect_result


def _node(
    node_id: str,
    platform: Platform,
    *,
    features: frozenset[str] = frozenset({"build"}),
    toolchains: frozenset[str] = frozenset({"python"}),
    gpu: bool = False,
) -> ExecutionNode:
    return ExecutionNode(
        NodeIdentity(node_id, platform, "x86_64", f"instance-{node_id}"),
        NodeCapabilities(features, toolchains, gpu),
        ResourceEnvelope(8, 16384, 65536),
    )


def _authority(*node_ids: str) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        r"products\Ніка Core\build",
        tuple(node_ids),
        ("pypi.org:443",),
        ("credref:package-index",),
    )


def _spec(
    work_id: str,
    platform: Platform,
    *node_ids: str,
    require_gpu: bool = False,
    features: frozenset[str] = frozenset({"build"}),
) -> BuildExecutionSpec:
    return BuildExecutionSpec(
        ExecutionRequest(
            "project-1",
            work_id,
            platform,
            features,
            frozenset({"python"}),
            ResourceEnvelope(2, 2048, 4096),
            require_gpu,
        ),
        SHA,
        ("python", "-m", "build"),
        _authority(*node_ids),
        120,
    )


def _result(
    *,
    succeeded: bool = True,
    uncertain: bool = False,
    source_sha: str = SHA,
    digest: str = DIGEST,
    ref: str = "evidence://build",
) -> BuildExecutionResult:
    return BuildExecutionResult(source_sha, digest, succeeded, uncertain, (ref,), NOW)


def _coordinator(
    *nodes: ExecutionNode,
    unavailable: set[str] | None = None,
) -> tuple[BuildExecutionCoordinator, ExecutionNodeRegistry]:
    registry = ExecutionNodeRegistry()
    for node in nodes:
        registry.register(node)
    coordinator = BuildExecutionCoordinator(registry, FakeAvailability(unavailable or set()))
    return coordinator, registry


def test_project_execution_authority_normalizes_windows_path_and_keeps_opaque_refs() -> None:
    authority = _authority("win-1")

    assert authority.workspace_relpath == "products/Ніка Core/build"
    assert authority.credential_refs == ("credref:package-index",)
    assert authority.network_scopes == ("pypi.org:443",)


@pytest.mark.parametrize(
    ("path", "credential_refs", "network_scopes", "message"),
    [
        ("../outside", ("credref:a",), (), "unsafe segment"),
        (r"C:\\outside", ("credref:a",), (), "project-relative"),
        (r"\\\\server\\share", ("credref:a",), (), "project-relative"),
        ("safe/path", ("raw-password",), (), "credref"),
        ("safe/path", ("credref:a",), ("*",), "wildcard"),
    ],
)
def test_project_execution_authority_rejects_scope_escape(
    path: str,
    credential_refs: tuple[str, ...],
    network_scopes: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(BuildExecutionError, match=message):
        ProjectExecutionAuthority(
            "project-1",
            "repo-main",
            path,
            ("node-1",),
            network_scopes,
            credential_refs,
        )


def test_windows_and_linux_return_same_normalized_evidence_contract() -> None:
    coordinator, _ = _coordinator(
        _node("win-1", Platform.WINDOWS),
        _node("linux-1", Platform.LINUX),
    )
    windows = _spec("work-win", Platform.WINDOWS, "win-1", "linux-1")
    linux = _spec("work-linux", Platform.LINUX, "win-1", "linux-1")

    for spec, ref in ((windows, "evidence://windows"), (linux, "evidence://linux")):
        coordinator.submit(spec, now=NOW)
        coordinator.prepare(spec.request.work_id, now=NOW)
        coordinator.begin_dispatch(spec.request.work_id, now=NOW)
        record = coordinator.run_dispatch(
            spec.request.work_id,
            FakePort(run_result=_result(ref=ref)),
            now=NOW,
        )
        assert record.state is BuildExecutionState.SUCCEEDED
        assert record.evidence is not None
        assert record.evidence.work_id == spec.request.work_id
        assert record.evidence.release_sha == SHA
        assert record.evidence.succeeded is True

    assert coordinator.get("work-win").evidence.__class__ is coordinator.get(
        "work-linux"
    ).evidence.__class__


def test_macos_without_authorized_node_fails_closed_without_dispatch() -> None:
    coordinator, _ = _coordinator(_node("win-1", Platform.WINDOWS))
    spec = _spec("work-macos", Platform.MACOS, "win-1", features=frozenset())
    port = FakePort(run_result=_result())

    coordinator.submit(spec, now=NOW)
    waiting = coordinator.prepare("work-macos", now=NOW)

    assert waiting.state is BuildExecutionState.WAITING_FOR_NODE
    assert "macos" in (waiting.block_reason or "")
    assert waiting.evidence is None
    assert port.run_calls == 0


def test_gpu_request_routes_only_to_authorized_gpu_node() -> None:
    coordinator, _ = _coordinator(
        _node("cpu-1", Platform.LINUX),
        _node("gpu-1", Platform.LINUX, gpu=True),
    )
    spec = _spec("gpu-work", Platform.LINUX, "cpu-1", "gpu-1", require_gpu=True)

    coordinator.submit(spec, now=NOW)
    prepared = coordinator.prepare("gpu-work", now=NOW)

    assert prepared.state is BuildExecutionState.PREPARED
    assert prepared.node_id == "gpu-1"


def test_on_prem_feature_and_project_authorization_filter_routing() -> None:
    coordinator, _ = _coordinator(
        _node("a-cloud", Platform.LINUX, features=frozenset({"build", "on-prem"})),
        _node("b-onprem", Platform.LINUX, features=frozenset({"build", "on-prem"})),
    )
    spec = _spec(
        "onprem-work",
        Platform.LINUX,
        "b-onprem",
        features=frozenset({"build", "on-prem"}),
    )

    coordinator.submit(spec, now=NOW)
    prepared = coordinator.prepare("onprem-work", now=NOW)

    assert prepared.node_id == "b-onprem"


def test_unavailable_first_node_reroutes_to_second_authorized_node() -> None:
    coordinator, registry = _coordinator(
        _node("a-linux", Platform.LINUX),
        _node("b-linux", Platform.LINUX),
        unavailable={"a-linux"},
    )
    spec = _spec("work-1", Platform.LINUX, "a-linux", "b-linux")

    coordinator.submit(spec, now=NOW)
    prepared = coordinator.prepare("work-1", now=NOW)

    assert prepared.node_id == "b-linux"
    leases = registry.snapshot().leases
    assert len(leases) == 1
    assert leases[0].node_id == "b-linux"


def test_busy_authorized_node_waits_then_retries_after_capacity_returns() -> None:
    coordinator, registry = _coordinator(_node("linux-1", Platform.LINUX))
    blocker = registry.acquire(
        _spec("blocker", Platform.LINUX, "linux-1").request,
        now=NOW,
        lease_seconds=30,
    )
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    coordinator.submit(spec, now=NOW)

    waiting = coordinator.prepare("work-1", now=NOW)
    registry.release(blocker.lease_id)
    prepared = coordinator.retry("work-1", now=NOW + timedelta(seconds=1))

    assert waiting.state is BuildExecutionState.WAITING_FOR_NODE
    assert prepared.state is BuildExecutionState.PREPARED
    assert prepared.node_id == "linux-1"


def test_node_loss_before_dispatch_returns_to_waiting_and_never_runs() -> None:
    availability = FakeAvailability(set())
    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1", Platform.LINUX))
    coordinator = BuildExecutionCoordinator(registry, availability)
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    availability.unavailable.add("linux-1")

    with pytest.raises(BuildExecutionError, match="became unavailable"):
        coordinator.begin_dispatch("work-1", now=NOW + timedelta(seconds=1))

    record = coordinator.get("work-1")
    assert record.state is BuildExecutionState.WAITING_FOR_NODE
    assert registry.snapshot().leases == ()


def test_uncertain_result_requires_inspection_and_never_replays_run() -> None:
    coordinator, _ = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    port = FakePort(
        run_result=_result(succeeded=False, uncertain=True, ref="evidence://uncertain"),
        inspect_result=_result(ref="evidence://inspect"),
    )
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    coordinator.begin_dispatch("work-1", now=NOW)

    uncertain = coordinator.run_dispatch("work-1", port, now=NOW)
    finished = coordinator.reconcile("work-1", port, now=NOW + timedelta(seconds=1))

    assert uncertain.state is BuildExecutionState.RECONCILE_REQUIRED
    assert finished.state is BuildExecutionState.SUCCEEDED
    assert port.run_calls == 1
    assert port.inspect_calls == 1


def test_node_port_exception_reconciles_without_second_run() -> None:
    coordinator, _ = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    port = FakePort(
        raise_on_run=True,
        inspect_result=_result(ref="evidence://inspect-after-loss"),
    )
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    coordinator.begin_dispatch("work-1", now=NOW)

    uncertain = coordinator.run_dispatch("work-1", port, now=NOW)
    finished = coordinator.reconcile("work-1", port, now=NOW + timedelta(seconds=1))

    assert uncertain.state is BuildExecutionState.RECONCILE_REQUIRED
    assert finished.state is BuildExecutionState.SUCCEEDED
    assert port.run_calls == 1
    assert port.inspect_calls == 1


def test_prepared_snapshot_restores_exact_unexpired_registry_lease() -> None:
    coordinator, registry = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    coordinator.submit(spec, now=NOW)
    prepared = coordinator.prepare("work-1", now=NOW)
    coordinator_snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry_snapshot)
    restarted = BuildExecutionCoordinator(restarted_registry, FakeAvailability(set()))
    restarted.restore(coordinator_snapshot, now=NOW + timedelta(seconds=1))

    restored = restarted.get("work-1")
    assert restored == prepared
    dispatch = restarted.begin_dispatch("work-1", now=NOW + timedelta(seconds=2))
    assert dispatch.node_id == "linux-1"


def test_restart_after_dispatch_boundary_requires_inspection_only() -> None:
    coordinator, registry = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    dispatch = coordinator.begin_dispatch("work-1", now=NOW)
    coordinator_snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry_snapshot)
    restarted = BuildExecutionCoordinator(restarted_registry, FakeAvailability(set()))
    restarted.restore(coordinator_snapshot, now=NOW + timedelta(seconds=1))
    port = FakePort(inspect_result=_result(ref="evidence://restart-inspect"))
    finished = restarted.reconcile("work-1", port, now=NOW + timedelta(seconds=2))

    assert restarted_registry.snapshot().leases == ()
    assert finished.state is BuildExecutionState.SUCCEEDED
    assert finished.dispatch == dispatch
    assert port.run_calls == 0
    assert port.inspect_calls == 1


def test_expired_prepared_lease_restores_as_waiting_for_node() -> None:
    coordinator, registry = _coordinator(_node("linux-1", Platform.LINUX))
    spec = replace(_spec("work-1", Platform.LINUX, "linux-1"), lease_seconds=1)
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    coordinator_snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()

    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry_snapshot)
    restarted = BuildExecutionCoordinator(restarted_registry, FakeAvailability(set()))
    restarted.restore(coordinator_snapshot, now=NOW + timedelta(seconds=2))

    record = restarted.get("work-1")
    assert record.state is BuildExecutionState.WAITING_FOR_NODE
    assert record.node_id is None
    assert restarted_registry.snapshot().leases == ()


def test_terminal_snapshot_with_forged_evidence_fails_closed() -> None:
    coordinator, registry = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    coordinator.begin_dispatch("work-1", now=NOW)
    good = coordinator.run_dispatch(
        "work-1",
        FakePort(run_result=_result()),
        now=NOW,
    )
    assert good.evidence is not None
    forged_evidence = replace(good.evidence, node_id="other-node")
    forged_record = replace(good, evidence=forged_evidence)

    restarted = BuildExecutionCoordinator(registry, FakeAvailability(set()))
    with pytest.raises(BuildExecutionError, match="evidence does not match"):
        restarted.restore(
            replace(coordinator.snapshot(), records=(forged_record,)),
            now=NOW + timedelta(seconds=1),
        )


def test_duplicate_work_is_idempotent_but_changed_payload_is_rejected() -> None:
    coordinator, _ = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")

    first = coordinator.submit(spec, now=NOW)
    second = coordinator.submit(spec, now=NOW + timedelta(seconds=1))
    changed = replace(spec, argv=("python", "-m", "pytest"))

    assert first == second
    with pytest.raises(BuildExecutionError, match="conflicts with prior execution payload"):
        coordinator.submit(changed, now=NOW)


def test_wrong_source_sha_result_never_becomes_success_and_releases_capacity() -> None:
    coordinator, registry = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    coordinator.begin_dispatch("work-1", now=NOW)

    record = coordinator.run_dispatch(
        "work-1",
        FakePort(run_result=_result(source_sha="b" * 40)),
        now=NOW,
    )

    assert record.state is BuildExecutionState.RECONCILE_REQUIRED
    assert record.evidence is None
    assert "SHA mismatch" in (record.block_reason or "")
    assert registry.snapshot().leases == ()


def test_restore_rejects_duplicate_registry_leases_for_same_project_work() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1", Platform.LINUX))
    registry.register(_node("linux-2", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1", "linux-2")
    registry.acquire(spec.request, now=NOW, lease_seconds=120)
    registry.acquire(spec.request, now=NOW, lease_seconds=120)
    source = BuildExecutionCoordinator(registry, FakeAvailability(set()))
    source.submit(spec, now=NOW)

    restarted = BuildExecutionCoordinator(registry, FakeAvailability(set()))
    with pytest.raises(BuildExecutionError, match="duplicate active leases"):
        restarted.restore(source.snapshot(), now=NOW + timedelta(seconds=1))


def test_restore_rejects_prepared_record_with_forged_dispatch_identity() -> None:
    coordinator, registry = _coordinator(_node("linux-1", Platform.LINUX))
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    coordinator.submit(spec, now=NOW)
    prepared = coordinator.prepare("work-1", now=NOW)
    forged_dispatch = BuildExecutionDispatch(
        "dispatch:project-1:work-1:1",
        "project-1",
        "work-1",
        "linux-1",
        Platform.LINUX,
        SHA,
        spec.argv,
        spec.authority,
        1,
    )
    forged = replace(prepared, dispatch=forged_dispatch)

    restarted = BuildExecutionCoordinator(registry, FakeAvailability(set()))
    with pytest.raises(BuildExecutionError, match="pre-dispatch"):
        restarted.restore(
            replace(coordinator.snapshot(), records=(forged,)),
            now=NOW + timedelta(seconds=1),
        )
