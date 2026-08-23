from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionCoordinator,
    BuildExecutionDispatch,
    BuildExecutionError,
    BuildExecutionNodePort,
    BuildExecutionPortError,
    BuildExecutionResult,
    BuildExecutionScopeRequest,
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
class FakeAuthority:
    authorities: dict[tuple[str, str, str], ProjectExecutionAuthority]

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> ProjectExecutionAuthority:
        try:
            return self.authorities[(project_id, repository_id, work_id)]
        except KeyError as exc:
            raise BuildExecutionError("no trusted execution authority for work") from exc


@dataclass
class FakePort(BuildExecutionNodePort):
    run_result: BuildExecutionResult | None = None
    inspect_result: BuildExecutionResult | None = None
    raise_port_error: bool = False
    raise_programming_error: bool = False
    run_calls: int = 0
    inspect_calls: int = 0

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        self.run_calls += 1
        if self.raise_port_error:
            raise BuildExecutionPortError("simulated transport loss")
        if self.raise_programming_error:
            raise ValueError("simulated adapter bug")
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


def _authority(
    work_id: str,
    *node_ids: str,
    permissions: frozenset[str] = frozenset({"build_release"}),
    workspace_paths: tuple[str, ...] = ("products/Ніка Core",),
    network_scopes: tuple[str, ...] = ("pypi.org:443",),
    credential_refs: tuple[str, ...] = ("credref:package-index",),
    commands: tuple[ApprovedBuildCommand, ...] = (
        ApprovedBuildCommand("build", ("python", "-m", "build")),
    ),
) -> ProjectExecutionAuthority:
    return ProjectExecutionAuthority(
        "project-1",
        "repo-main",
        work_id,
        permissions,
        tuple(node_ids),
        workspace_paths,
        network_scopes,
        credential_refs,
        commands,
        ("authority://team-plan/1",),
    )


def _scope(
    *node_ids: str,
    repository_id: str = "repo-main",
    workspace_relpath: str = r"products\Ніка Core\build",
    network_scopes: tuple[str, ...] = ("pypi.org:443",),
    credential_refs: tuple[str, ...] = ("credref:package-index",),
    command_id: str = "build",
) -> BuildExecutionScopeRequest:
    return BuildExecutionScopeRequest(
        repository_id,
        workspace_relpath,
        tuple(node_ids),
        network_scopes,
        credential_refs,
        command_id,
    )


def _spec(
    work_id: str,
    platform: Platform,
    *node_ids: str,
    require_gpu: bool = False,
    features: frozenset[str] = frozenset({"build"}),
    scope: BuildExecutionScopeRequest | None = None,
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
        scope or _scope(*node_ids),
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
    spec: BuildExecutionSpec,
    authority: ProjectExecutionAuthority,
    *nodes: ExecutionNode,
    unavailable: set[str] | None = None,
) -> tuple[BuildExecutionCoordinator, ExecutionNodeRegistry, FakeAuthority]:
    registry = ExecutionNodeRegistry()
    for node in nodes:
        registry.register(node)
    trusted = FakeAuthority(
        {(spec.request.project_id, spec.scope.repository_id, spec.request.work_id): authority}
    )
    coordinator = BuildExecutionCoordinator(
        registry,
        FakeAvailability(unavailable or set()),
        trusted,
    )
    return coordinator, registry, trusted


def _prepare_linux(work_id: str = "work-1"):
    spec = _spec(work_id, Platform.LINUX, "linux-1")
    authority = _authority(work_id, "linux-1")
    coordinator, registry, trusted = _coordinator(
        spec,
        authority,
        _node("linux-1", Platform.LINUX),
    )
    coordinator.submit(spec, now=NOW)
    coordinator.prepare(work_id, now=NOW)
    return coordinator, registry, trusted, spec


def test_candidate_scope_contains_no_authority_or_argv_and_dispatch_uses_host_command() -> None:
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    authority = _authority(
        "work-1",
        "linux-1",
        commands=(ApprovedBuildCommand("build", ("python", "-m", "build", "--wheel")),),
    )
    coordinator, _, _ = _coordinator(spec, authority, _node("linux-1", Platform.LINUX))

    submitted = coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    dispatch = coordinator.begin_dispatch("work-1", now=NOW)

    assert not hasattr(spec, "authority")
    assert not hasattr(spec, "argv")
    assert submitted.grant.argv == ("python", "-m", "build", "--wheel")
    assert dispatch.grant.argv == submitted.grant.argv
    assert dispatch.grant.authority_evidence_refs == ("authority://team-plan/1",)


@pytest.mark.parametrize(
    "executable",
    ["cmd.exe", "powershell.exe", "pwsh", "bash", "/bin/sh", "wsl.exe"],
)
def test_trusted_command_policy_rejects_generic_shells(executable: str) -> None:
    with pytest.raises(BuildExecutionError, match="generic shell"):
        ApprovedBuildCommand("build", (executable, "echo", "unsafe"))


def test_submit_requires_trusted_team_build_permission() -> None:
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    authority = _authority("work-1", "linux-1", permissions=frozenset({"run_tests"}))
    coordinator, _, _ = _coordinator(spec, authority, _node("linux-1", Platform.LINUX))

    with pytest.raises(BuildExecutionError, match="permission ceiling"):
        coordinator.submit(spec, now=NOW)


@pytest.mark.parametrize(
    ("scope", "authority", "message"),
    [
        (
            _scope("linux-2"),
            _authority("work-1", "linux-1"),
            "node exceeds",
        ),
        (
            _scope("linux-1", network_scopes=("internal.example:443",)),
            _authority("work-1", "linux-1"),
            "network scope exceeds",
        ),
        (
            _scope("linux-1", credential_refs=("credref:production-signing",)),
            _authority("work-1", "linux-1"),
            "credential exceeds",
        ),
        (
            _scope("linux-1", workspace_relpath="products/Ніка Core-secret"),
            _authority("work-1", "linux-1"),
            "workspace path exceeds",
        ),
        (
            _scope("linux-1", workspace_relpath="PRODUCTS/Ніка Core/build"),
            _authority("work-1", "linux-1"),
            "workspace path exceeds",
        ),
        (
            _scope("linux-1", command_id="release"),
            _authority("work-1", "linux-1"),
            "command is not host-approved",
        ),
    ],
)
def test_candidate_cannot_expand_trusted_execution_authority(
    scope: BuildExecutionScopeRequest,
    authority: ProjectExecutionAuthority,
    message: str,
) -> None:
    spec = _spec("work-1", Platform.LINUX, "linux-1", scope=scope)
    coordinator, _, _ = _coordinator(spec, authority, _node("linux-1", Platform.LINUX))

    with pytest.raises(BuildExecutionError, match=message):
        coordinator.submit(spec, now=NOW)


def test_wrong_host_authority_work_identity_is_rejected() -> None:
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    wrong = _authority("work-2", "linux-1")
    trusted = FakeAuthority({("project-1", "repo-main", "work-1"): wrong})
    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1", Platform.LINUX))
    coordinator = BuildExecutionCoordinator(registry, FakeAvailability(set()), trusted)

    with pytest.raises(BuildExecutionError, match="wrong work identity"):
        coordinator.submit(spec, now=NOW)


def test_windows_and_linux_return_same_normalized_evidence_contract() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(_node("win-1", Platform.WINDOWS))
    registry.register(_node("linux-1", Platform.LINUX))
    win_spec = _spec("work-win", Platform.WINDOWS, "win-1")
    linux_spec = _spec("work-linux", Platform.LINUX, "linux-1")
    trusted = FakeAuthority(
        {
            ("project-1", "repo-main", "work-win"): _authority("work-win", "win-1"),
            ("project-1", "repo-main", "work-linux"): _authority(
                "work-linux", "linux-1"
            ),
        }
    )
    coordinator = BuildExecutionCoordinator(registry, FakeAvailability(set()), trusted)

    for spec, ref in (
        (win_spec, "evidence://windows"),
        (linux_spec, "evidence://linux"),
    ):
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
        assert record.evidence.release_sha == SHA

    assert coordinator.get("work-win").evidence.__class__ is coordinator.get(
        "work-linux"
    ).evidence.__class__


def test_macos_without_matching_node_waits_without_fake_success() -> None:
    spec = _spec("work-macos", Platform.MACOS, "mac-1", features=frozenset())
    authority = _authority("work-macos", "mac-1")
    coordinator, _, _ = _coordinator(spec, authority, _node("win-1", Platform.WINDOWS))
    coordinator.submit(spec, now=NOW)

    waiting = coordinator.prepare("work-macos", now=NOW)

    assert waiting.state is BuildExecutionState.WAITING_FOR_NODE
    assert waiting.evidence is None
    assert "macos" in (waiting.block_reason or "")


def test_gpu_request_routes_only_to_authorized_gpu_node() -> None:
    spec = _spec("gpu-work", Platform.LINUX, "gpu-1", require_gpu=True)
    authority = _authority("gpu-work", "gpu-1")
    coordinator, _, _ = _coordinator(
        spec,
        authority,
        _node("cpu-1", Platform.LINUX),
        _node("gpu-1", Platform.LINUX, gpu=True),
    )
    coordinator.submit(spec, now=NOW)

    prepared = coordinator.prepare("gpu-work", now=NOW)

    assert prepared.node_id == "gpu-1"


def test_unavailable_authorized_node_reroutes_and_releases_skipped_lease() -> None:
    spec = _spec("work-1", Platform.LINUX, "a-linux", "b-linux")
    authority = _authority("work-1", "a-linux", "b-linux")
    coordinator, registry, _ = _coordinator(
        spec,
        authority,
        _node("a-linux", Platform.LINUX),
        _node("b-linux", Platform.LINUX),
        unavailable={"a-linux"},
    )
    coordinator.submit(spec, now=NOW)

    prepared = coordinator.prepare("work-1", now=NOW)

    assert prepared.node_id == "b-linux"
    assert [lease.node_id for lease in registry.snapshot().leases] == ["b-linux"]


def test_busy_authorized_node_waits_then_retries_after_capacity_returns() -> None:
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    authority = _authority("work-1", "linux-1")
    coordinator, registry, _ = _coordinator(
        spec,
        authority,
        _node("linux-1", Platform.LINUX),
    )
    blocker = registry.acquire(
        _spec("blocker", Platform.LINUX, "linux-1").request,
        now=NOW,
        lease_seconds=30,
    )
    coordinator.submit(spec, now=NOW)

    waiting = coordinator.prepare("work-1", now=NOW)
    registry.release(blocker.lease_id)
    prepared = coordinator.retry("work-1", now=NOW + timedelta(seconds=1))

    assert waiting.state is BuildExecutionState.WAITING_FOR_NODE
    assert prepared.state is BuildExecutionState.PREPARED


def test_authority_change_before_dispatch_blocks_without_external_effect() -> None:
    coordinator, registry, trusted, spec = _prepare_linux()
    trusted.authorities[("project-1", "repo-main", "work-1")] = _authority(
        "work-1",
        "other-node",
    )

    with pytest.raises(BuildExecutionError, match="authority changed"):
        coordinator.begin_dispatch("work-1", now=NOW + timedelta(seconds=1))

    record = coordinator.get("work-1")
    assert record.state is BuildExecutionState.WAITING_FOR_AUTHORITY
    assert registry.snapshot().leases == ()
    assert record.dispatch is None
    assert record.spec == spec


def test_authority_change_after_dispatch_but_before_run_never_calls_port() -> None:
    coordinator, registry, trusted, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)
    trusted.authorities[("project-1", "repo-main", "work-1")] = _authority(
        "work-1",
        "other-node",
    )
    port = FakePort(run_result=_result())

    blocked = coordinator.run_dispatch("work-1", port, now=NOW + timedelta(seconds=1))

    assert blocked.state is BuildExecutionState.WAITING_FOR_AUTHORITY
    assert port.run_calls == 0
    assert registry.snapshot().leases == ()


def test_node_loss_before_dispatch_returns_to_waiting_without_run() -> None:
    spec = _spec("work-1", Platform.LINUX, "linux-1")
    authority = _authority("work-1", "linux-1")
    availability = FakeAvailability(set())
    registry = ExecutionNodeRegistry()
    registry.register(_node("linux-1", Platform.LINUX))
    trusted = FakeAuthority({("project-1", "repo-main", "work-1"): authority})
    coordinator = BuildExecutionCoordinator(registry, availability, trusted)
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    availability.unavailable.add("linux-1")

    with pytest.raises(BuildExecutionError, match="became unavailable"):
        coordinator.begin_dispatch("work-1", now=NOW + timedelta(seconds=1))

    assert coordinator.get("work-1").state is BuildExecutionState.WAITING_FOR_NODE
    assert registry.snapshot().leases == ()


def test_uncertain_result_requires_inspection_and_never_replays_run() -> None:
    coordinator, _, _, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)
    port = FakePort(
        run_result=_result(succeeded=False, uncertain=True, ref="evidence://uncertain"),
        inspect_result=_result(ref="evidence://inspect"),
    )

    uncertain = coordinator.run_dispatch("work-1", port, now=NOW)
    finished = coordinator.reconcile("work-1", port, now=NOW + timedelta(seconds=1))

    assert uncertain.state is BuildExecutionState.RECONCILE_REQUIRED
    assert finished.state is BuildExecutionState.SUCCEEDED
    assert port.run_calls == 1
    assert port.inspect_calls == 1


def test_normalized_port_error_reconciles_without_second_run() -> None:
    coordinator, _, _, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)
    port = FakePort(
        raise_port_error=True,
        inspect_result=_result(ref="evidence://inspect-after-loss"),
    )

    uncertain = coordinator.run_dispatch("work-1", port, now=NOW)
    finished = coordinator.reconcile("work-1", port, now=NOW + timedelta(seconds=1))

    assert uncertain.state is BuildExecutionState.RECONCILE_REQUIRED
    assert finished.state is BuildExecutionState.SUCCEEDED
    assert port.run_calls == 1
    assert port.inspect_calls == 1


def test_unexpected_adapter_bug_cannot_cause_blind_effect_replay() -> None:
    coordinator, _, _, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)
    port = FakePort(
        raise_programming_error=True,
        inspect_result=_result(ref="evidence://inspect-after-bug"),
    )

    with pytest.raises(ValueError, match="adapter bug"):
        coordinator.run_dispatch("work-1", port, now=NOW)

    assert coordinator.get("work-1").state is BuildExecutionState.EFFECT_IN_FLIGHT
    with pytest.raises(BuildExecutionError, match="never replayed"):
        coordinator.run_dispatch("work-1", port, now=NOW + timedelta(seconds=1))
    finished = coordinator.reconcile("work-1", port, now=NOW + timedelta(seconds=2))
    assert finished.state is BuildExecutionState.SUCCEEDED
    assert port.run_calls == 1
    assert port.inspect_calls == 1


def test_prepared_snapshot_restores_exact_unexpired_registry_lease() -> None:
    coordinator, registry, trusted, _ = _prepare_linux()
    prepared = coordinator.get("work-1")
    coordinator_snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()
    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry_snapshot)
    restarted = BuildExecutionCoordinator(
        restarted_registry,
        FakeAvailability(set()),
        trusted,
    )

    restarted.restore(coordinator_snapshot, now=NOW + timedelta(seconds=1))

    assert restarted.get("work-1") == prepared
    assert restarted.begin_dispatch("work-1", now=NOW + timedelta(seconds=2)).node_id == "linux-1"


@pytest.mark.parametrize(
    "state",
    [BuildExecutionState.DISPATCHING, BuildExecutionState.EFFECT_IN_FLIGHT],
)
def test_restart_after_effect_boundary_requires_inspection_only(state: BuildExecutionState) -> None:
    coordinator, registry, trusted, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)
    if state is BuildExecutionState.EFFECT_IN_FLIGHT:
        port = FakePort(raise_programming_error=True)
        with pytest.raises(ValueError):
            coordinator.run_dispatch("work-1", port, now=NOW)
    snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()
    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry_snapshot)
    restarted = BuildExecutionCoordinator(restarted_registry, FakeAvailability(set()), trusted)

    restarted.restore(snapshot, now=NOW + timedelta(seconds=1))
    port = FakePort(inspect_result=_result(ref="evidence://restart-inspect"))
    finished = restarted.reconcile("work-1", port, now=NOW + timedelta(seconds=2))

    assert restarted_registry.snapshot().leases == ()
    assert finished.state is BuildExecutionState.SUCCEEDED
    assert port.run_calls == 0
    assert port.inspect_calls == 1


def test_expired_prepared_lease_restores_as_waiting_for_node() -> None:
    spec = replace(_spec("work-1", Platform.LINUX, "linux-1"), lease_seconds=1)
    authority = _authority("work-1", "linux-1")
    coordinator, registry, trusted = _coordinator(
        spec,
        authority,
        _node("linux-1", Platform.LINUX),
    )
    coordinator.submit(spec, now=NOW)
    coordinator.prepare("work-1", now=NOW)
    snapshot = coordinator.snapshot()
    registry_snapshot = registry.snapshot()
    restarted_registry = ExecutionNodeRegistry()
    restarted_registry.restore(registry_snapshot)
    restarted = BuildExecutionCoordinator(restarted_registry, FakeAvailability(set()), trusted)

    restarted.restore(snapshot, now=NOW + timedelta(seconds=2))

    assert restarted.get("work-1").state is BuildExecutionState.WAITING_FOR_NODE
    assert restarted_registry.snapshot().leases == ()


def test_forged_snapshot_grant_fails_against_independent_host_authority() -> None:
    coordinator, registry, trusted, _ = _prepare_linux()
    record = coordinator.get("work-1")
    forged_grant = replace(
        record.grant,
        allowed_node_ids=("linux-1", "attacker-node"),
        network_scopes=("pypi.org:443", "internal.example:443"),
    )
    forged = replace(record, grant=forged_grant)
    restarted = BuildExecutionCoordinator(registry, FakeAvailability(set()), trusted)

    with pytest.raises(BuildExecutionError, match="trusted host authority"):
        restarted.restore(replace(coordinator.snapshot(), records=(forged,)), now=NOW)


def test_trusted_authority_change_across_restart_fails_closed() -> None:
    coordinator, registry, trusted, _ = _prepare_linux()
    snapshot = coordinator.snapshot()
    trusted.authorities[("project-1", "repo-main", "work-1")] = _authority(
        "work-1",
        "linux-1",
        credential_refs=(),
    )
    restarted = BuildExecutionCoordinator(registry, FakeAvailability(set()), trusted)

    with pytest.raises(BuildExecutionError, match="trusted host authority"):
        restarted.restore(snapshot, now=NOW)


def test_terminal_snapshot_with_forged_evidence_fails_closed() -> None:
    coordinator, registry, trusted, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)
    good = coordinator.run_dispatch("work-1", FakePort(run_result=_result()), now=NOW)
    assert good.evidence is not None
    forged = replace(good, evidence=replace(good.evidence, node_id="other-node"))
    restarted = BuildExecutionCoordinator(registry, FakeAvailability(set()), trusted)

    with pytest.raises(BuildExecutionError, match="evidence does not match"):
        restarted.restore(replace(coordinator.snapshot(), records=(forged,)), now=NOW)


def test_source_sha_mismatch_never_becomes_success() -> None:
    coordinator, _, _, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)

    record = coordinator.run_dispatch(
        "work-1",
        FakePort(run_result=_result(source_sha="b" * 40)),
        now=NOW,
    )

    assert record.state is BuildExecutionState.RECONCILE_REQUIRED
    assert record.evidence is None


@pytest.mark.parametrize("bad_value", [True, False, 1.5, "1"])
def test_durable_attempt_identity_rejects_non_integer_values(bad_value: object) -> None:
    with pytest.raises(BuildExecutionError, match="attempt"):
        replace(
            BuildExecutionDispatch(
                "dispatch:project-1:work-1:1",
                "project-1",
                "work-1",
                "linux-1",
                Platform.LINUX,
                SHA,
                _grant_for_scalar_test(),
                1,
            ),
            attempt=bad_value,
        )


def _grant_for_scalar_test():
    from nika_core.product_factory_build_execution import ExecutionGrant

    return ExecutionGrant(
        "project-1",
        "repo-main",
        "work-1",
        "products/app",
        ("linux-1",),
        (),
        (),
        "build",
        ("python", "-m", "build"),
        ("authority://1",),
    )


def test_prepare_after_dispatch_does_not_erase_effect_identity_on_authority_revocation() -> None:
    coordinator, _, trusted, _ = _prepare_linux()
    dispatch = coordinator.begin_dispatch("work-1", now=NOW)
    trusted.authorities[("project-1", "repo-main", "work-1")] = _authority(
        "work-1",
        "other-node",
    )

    record = coordinator.prepare("work-1", now=NOW + timedelta(seconds=1))

    assert record.state is BuildExecutionState.DISPATCHING
    assert record.dispatch == dispatch


def test_reconcile_remains_available_after_post_effect_authority_revocation() -> None:
    coordinator, _, trusted, _ = _prepare_linux()
    coordinator.begin_dispatch("work-1", now=NOW)
    broken = FakePort(raise_programming_error=True)
    with pytest.raises(ValueError):
        coordinator.run_dispatch("work-1", broken, now=NOW)
    trusted.authorities[("project-1", "repo-main", "work-1")] = _authority(
        "work-1",
        "other-node",
    )
    inspected = FakePort(inspect_result=_result(ref="evidence://revoked-inspection"))

    record = coordinator.reconcile("work-1", inspected, now=NOW + timedelta(seconds=1))

    assert record.state is BuildExecutionState.SUCCEEDED
    assert inspected.run_calls == 0
    assert inspected.inspect_calls == 1
