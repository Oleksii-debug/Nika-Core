from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.product_factory_deployment import (
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionRequest,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionRecord,
    DeploymentExecutionSnapshot,
    DeploymentExecutionSpec,
    OperationState,
)
from nika_core.product_factory_deployment_waves import (
    DeploymentWaveCoordinator,
    DeploymentWaveError,
    DeploymentWavePlan,
    DeploymentWaveSnapshot,
    RolloutState,
    ServiceRolloutSpec,
)


SHA_A = "a" * 40
DIGEST_A = "sha256:" + "1" * 64


def _execution(service: str, *, project: str = "social", wave: int = 0) -> ServiceRolloutSpec:
    request = ExecutionRequest(
        project_id=project,
        work_id=f"work-{service}",
        platform=Platform.LINUX,
        required_features=frozenset({"staging"}),
        required_toolchains=frozenset(),
        resources=ResourceEnvelope(1, 256, 512),
    )
    environment = EnvironmentIdentity(
        environment_id="shared-staging",
        project_id=project,
        tier=EnvironmentTier.STAGING,
        provider_ref="provider:staging",
    )
    release = ReleaseRef(project, f"1.0.{wave}", SHA_A, DIGEST_A)
    intent = DeploymentIntent(f"intent-{service}", project, environment, release)
    execution = DeploymentExecutionSpec(
        operation_id=f"operation-{service}",
        request=request,
        intent=intent,
        credential_ref="credential:staging",
        credential_audience="provider",
        credential_scope="deploy",
    )
    return ServiceRolloutSpec(service, wave, execution)


class _FakeExecutions:
    def __init__(self) -> None:
        self.records: dict[str, DeploymentExecutionRecord] = {}
        self.complete_state: dict[str, OperationState] = {}
        self.prepare_state: dict[str, OperationState] = {}
        self.reconcile_state: dict[str, OperationState] = {}
        self.complete_calls: list[str] = []

    def submit(self, spec: DeploymentExecutionSpec) -> DeploymentExecutionRecord:
        return self.records.setdefault(
            spec.operation_id,
            DeploymentExecutionRecord(spec, OperationState.PENDING),
        )

    def get(self, operation_id: str) -> DeploymentExecutionRecord:
        return self.records[operation_id]

    def prepare(self, operation_id: str) -> DeploymentExecutionRecord:
        record = self.records[operation_id]
        state = self.prepare_state.get(operation_id, OperationState.PREPARED)
        record = replace(record, state=state, attempt=record.attempt + 1)
        self.records[operation_id] = record
        return record

    def retry(self, operation_id: str) -> DeploymentExecutionRecord:
        return self.prepare(operation_id)

    def complete(self, operation_id: str) -> DeploymentExecutionRecord:
        self.complete_calls.append(operation_id)
        record = self.records[operation_id]
        state = self.complete_state.get(operation_id, OperationState.SUCCEEDED)
        deployment_state = {
            OperationState.SUCCEEDED: DeploymentState.HEALTHY,
            OperationState.REJECTED: DeploymentState.REJECTED,
            OperationState.ROLLED_BACK: DeploymentState.ROLLED_BACK,
            OperationState.RECONCILE_REQUIRED: DeploymentState.UNCERTAIN,
        }.get(state)
        record = replace(record, state=state, deployment_state=deployment_state)
        self.records[operation_id] = record
        return record

    def reconcile(self, operation_id: str) -> DeploymentExecutionRecord:
        record = self.records[operation_id]
        state = self.reconcile_state.get(operation_id, OperationState.SUCCEEDED)
        record = replace(record, state=state)
        self.records[operation_id] = record
        return record

    def snapshot(self) -> DeploymentExecutionSnapshot:
        records = []
        for operation_id in sorted(self.records):
            record = self.records[operation_id]
            state = (
                OperationState.RECOVERY_REQUIRED
                if record.state is OperationState.PREPARED
                else record.state
            )
            records.append(replace(record, state=state, node_id=None))
        return DeploymentExecutionSnapshot(tuple(records))

    def restore(self, snapshot: DeploymentExecutionSnapshot) -> None:
        self.records = {record.spec.operation_id: record for record in snapshot.records}


def _coordinator() -> tuple[DeploymentWaveCoordinator, _FakeExecutions]:
    executions = _FakeExecutions()
    return DeploymentWaveCoordinator(executions), executions  # type: ignore[arg-type]


def test_plan_rejects_unknown_or_same_wave_dependencies() -> None:
    service = _execution("api")
    unknown = replace(service, depends_on=("missing",))
    with pytest.raises(DeploymentWaveError):
        DeploymentWavePlan("plan", "social", (unknown,))

    db = _execution("db", wave=0)
    api = replace(_execution("api", wave=0), depends_on=("db",))
    with pytest.raises(DeploymentWaveError):
        DeploymentWavePlan("plan", "social", (db, api))


def test_submit_is_idempotent_and_conflict_safe() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    first = coordinator.submit(plan)
    assert coordinator.submit(plan) == first

    conflict = DeploymentWavePlan("plan", "social", (_execution("worker"),))
    with pytest.raises(DeploymentWaveError):
        coordinator.submit(conflict)


def test_later_wave_waits_for_dependencies() -> None:
    coordinator, executions = _coordinator()
    db = _execution("db", wave=0)
    api = replace(_execution("api", wave=1), depends_on=("db",))
    plan = DeploymentWavePlan("plan", "social", (api, db))
    coordinator.submit(plan)

    first = coordinator.advance("plan")
    assert {item.service_id: item.state for item in first.services} == {
        "db": OperationState.SUCCEEDED,
        "api": OperationState.PENDING,
    }
    assert executions.complete_calls == ["operation-db"]

    second = coordinator.advance("plan")
    assert second.state is RolloutState.SUCCEEDED
    assert executions.complete_calls == ["operation-db", "operation-api"]


def test_one_failed_service_does_not_corrupt_parallel_healthy_service() -> None:
    coordinator, executions = _coordinator()
    plan = DeploymentWavePlan(
        "plan",
        "social",
        (_execution("profiles"), _execution("messages")),
    )
    coordinator.submit(plan)
    executions.complete_state["operation-messages"] = OperationState.ROLLED_BACK

    result = coordinator.advance("plan")
    states = {item.service_id: item.state for item in result.services}
    assert states["profiles"] is OperationState.SUCCEEDED
    assert states["messages"] is OperationState.ROLLED_BACK
    assert result.state is RolloutState.PARTIAL_FAILURE


@pytest.mark.parametrize(
    "blocked_state",
    [OperationState.WAITING_FOR_NODE, OperationState.BLOCKED_CREDENTIAL],
)
def test_node_or_credential_block_pauses_without_touching_other_wave(
    blocked_state: OperationState,
) -> None:
    coordinator, executions = _coordinator()
    search = _execution("search", wave=0)
    feed = replace(_execution("feed", wave=1), depends_on=("search",))
    coordinator.submit(DeploymentWavePlan("plan", "social", (search, feed)))
    executions.prepare_state["operation-search"] = blocked_state

    result = coordinator.advance("plan")
    states = {item.service_id: item.state for item in result.services}
    assert states == {"search": blocked_state, "feed": OperationState.PENDING}
    assert result.state is RolloutState.PAUSED
    assert executions.complete_calls == []


def test_uncertain_provider_is_reconciled_without_second_complete() -> None:
    coordinator, executions = _coordinator()
    coordinator.submit(DeploymentWavePlan("plan", "social", (_execution("media"),)))
    executions.complete_state["operation-media"] = OperationState.RECONCILE_REQUIRED
    executions.reconcile_state["operation-media"] = OperationState.SUCCEEDED

    result = coordinator.advance("plan")
    assert result.state is RolloutState.SUCCEEDED
    assert executions.complete_calls == ["operation-media"]


def test_snapshot_restart_converts_prepared_execution_to_recovery_required() -> None:
    coordinator, executions = _coordinator()
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    coordinator.submit(plan)
    executions.prepare("operation-api")

    snapshot = coordinator.snapshot()
    execution = snapshot.execution.records[0]
    assert execution.state is OperationState.RECOVERY_REQUIRED

    restored, _ = _coordinator()
    restored.restore(snapshot)
    result = restored.advance("plan")
    assert result.state is RolloutState.SUCCEEDED


def test_restore_rejects_wave_state_that_disagrees_with_execution_state() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    submitted = coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    bad_service = replace(submitted.services[0], state=OperationState.SUCCEEDED)
    bad_plan = replace(submitted, services=(bad_service,))
    corrupted = DeploymentWaveSnapshot((bad_plan,), snapshot.execution)

    restored, _ = _coordinator()
    with pytest.raises(DeploymentWaveError):
        restored.restore(corrupted)


def test_sixty_service_three_wave_restart_scale_is_deterministic() -> None:
    coordinator, _ = _coordinator()
    services = []
    for index in range(60):
        wave = index // 20
        service = _execution(f"service-{index:02d}", wave=wave)
        if wave:
            service = replace(service, depends_on=(f"service-{index - 20:02d}",))
        services.append(service)
    plan = DeploymentWavePlan("scale-plan", "social", tuple(services))
    coordinator.submit(plan)

    first = coordinator.advance("scale-plan")
    assert sum(item.state is OperationState.SUCCEEDED for item in first.services) == 20

    snapshot = coordinator.snapshot()
    restored, _ = _coordinator()
    restored.restore(snapshot)
    second = restored.advance("scale-plan")
    third = restored.advance("scale-plan")

    assert sum(item.state is OperationState.SUCCEEDED for item in second.services) == 40
    assert third.state is RolloutState.SUCCEEDED
    assert all(item.state is OperationState.SUCCEEDED for item in third.services)
