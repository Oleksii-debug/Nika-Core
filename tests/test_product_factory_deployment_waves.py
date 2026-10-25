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
    DeploymentExecutionError,
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
DIGEST_A = "1" * 64


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
        existing = self.records.get(spec.operation_id)
        if existing is not None:
            if existing.spec != spec:
                raise DeploymentExecutionError(
                    "operation id conflicts with prior deployment payload"
                )
            return existing
        record = DeploymentExecutionRecord(spec, OperationState.PENDING)
        self.records[spec.operation_id] = record
        return record

    def get(self, operation_id: str) -> DeploymentExecutionRecord:
        record = self.records.get(operation_id)
        if record is None:
            raise DeploymentExecutionError("unknown deployment execution operation")
        return record

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


@pytest.mark.parametrize("wave", [True, False, 1.5, float("nan"), float("inf"), "1"])
def test_plan_rejects_ambiguous_wave_index(wave: object) -> None:
    with pytest.raises(DeploymentWaveError, match="nonnegative integer"):
        replace(_execution("api"), wave=wave)


def test_restore_rejects_swapped_service_ids_before_mutating_execution() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"), _execution("db")))
    source, _ = _coordinator()
    source.submit(plan)
    snapshot = source.snapshot()
    original = snapshot.plans[0]
    first, second = original.services
    swapped = (
        replace(first, service_id=second.service_id),
        replace(second, service_id=first.service_id),
    )
    corrupted = replace(snapshot, plans=(replace(original, services=swapped),))

    target, executions = _coordinator()
    target.submit(plan)
    before = target.snapshot()
    with pytest.raises(DeploymentWaveError, match="service identity"):
        target.restore(corrupted)
    assert target.snapshot() == before
    assert all(record.state is OperationState.PENDING for record in executions.records.values())


def test_restore_rejects_changed_wave_for_correct_operation() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    coordinator, _ = _coordinator()
    coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    record = snapshot.plans[0]
    bad = replace(record.services[0], wave=1)
    corrupted = replace(snapshot, plans=(replace(record, services=(bad,)),))

    target, _ = _coordinator()
    with pytest.raises(DeploymentWaveError, match="service identity"):
        target.restore(corrupted)


def test_restore_rejects_changed_execution_spec_with_same_operation_id() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    coordinator, _ = _coordinator()
    coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    execution = snapshot.execution.records[0]
    corrupted_spec = replace(execution.spec, credential_scope="unapproved:scope")
    corrupted_execution = replace(execution, spec=corrupted_spec)
    corrupted = replace(
        snapshot,
        execution=replace(snapshot.execution, records=(corrupted_execution,)),
    )

    target, executions = _coordinator()
    target.submit(plan)
    before = target.snapshot()
    with pytest.raises(DeploymentWaveError, match="execution specification"):
        target.restore(corrupted)
    assert target.snapshot() == before
    assert all(record.state is OperationState.PENDING for record in executions.records.values())


def test_restore_rejects_forged_successful_summary_with_pending_service() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    coordinator, _ = _coordinator()
    coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    forged = replace(snapshot.plans[0], state=RolloutState.SUCCEEDED)
    corrupted = replace(snapshot, plans=(forged,))

    target, _ = _coordinator()
    with pytest.raises(DeploymentWaveError, match="summary"):
        target.restore(corrupted)


def test_restore_rejects_ambiguous_attempt_even_when_python_equality_matches() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    coordinator, _ = _coordinator()
    coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    record = snapshot.plans[0]
    forged = replace(record.services[0], attempt=False)
    corrupted = replace(snapshot, plans=(replace(record, services=(forged,)),))

    target, _ = _coordinator()
    with pytest.raises(DeploymentWaveError, match="execution snapshot"):
        target.restore(corrupted)


@pytest.mark.parametrize("value", [None, 3, b"api", "", "  "])
def test_plan_rejects_noncanonical_identity_carriers(value: object) -> None:
    with pytest.raises(DeploymentWaveError, match="service identity"):
        replace(_execution("api"), service_id=value)
    with pytest.raises(DeploymentWaveError, match="rollout identity"):
        DeploymentWavePlan(value, "social", (_execution("api"),))
    with pytest.raises(DeploymentWaveError, match="rollout identity"):
        DeploymentWavePlan("plan", value, (_execution("api"),))


@pytest.mark.parametrize(
    "dependencies",
    [None, ["db"], ("",), (3,), (object(),), "db"],
)
def test_service_rejects_noncanonical_dependency_carriers(dependencies: object) -> None:
    with pytest.raises(DeploymentWaveError, match="dependencies"):
        replace(_execution("api"), depends_on=dependencies)


@pytest.mark.parametrize("services", [None, [], [_execution("api")], ("api",)])
def test_plan_rejects_noncanonical_service_collections(services: object) -> None:
    with pytest.raises(DeploymentWaveError, match="services|at least one"):
        DeploymentWavePlan("plan", "social", services)


def test_service_rejects_noncanonical_execution_before_plan_dispatch() -> None:
    with pytest.raises(DeploymentWaveError, match="execution spec"):
        replace(_execution("api"), execution=object())



def test_restore_rejects_missing_plan_before_identity_traversal() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    source, _ = _coordinator()
    source.submit(plan)
    snapshot = source.snapshot()
    corrupted_plan = replace(snapshot.plans[0], plan=None)
    corrupted = replace(snapshot, plans=(corrupted_plan,))

    target, executions = _coordinator()
    target.submit(plan)
    before = target.snapshot()

    with pytest.raises(DeploymentWaveError, match="plan structure"):
        target.restore(corrupted)
    assert target.snapshot() == before
    assert executions.records["operation-api"].state is OperationState.PENDING


def test_restore_rejects_malformed_service_record_before_field_traversal() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    source, _ = _coordinator()
    source.submit(plan)
    snapshot = source.snapshot()
    corrupted_plan = replace(snapshot.plans[0], services=(object(),))
    corrupted = replace(snapshot, plans=(corrupted_plan,))

    target, executions = _coordinator()
    target.submit(plan)
    before = target.snapshot()

    with pytest.raises(DeploymentWaveError, match="plan structure"):
        target.restore(corrupted)
    assert target.snapshot() == before
    assert executions.records["operation-api"].state is OperationState.PENDING


@pytest.mark.parametrize("records", [[], (object(),)])
def test_restore_rejects_malformed_execution_collection_before_traversal(
    records: object,
) -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    source, _ = _coordinator()
    source.submit(plan)
    snapshot = source.snapshot()
    corrupted_execution = replace(snapshot.execution, records=records)
    corrupted = replace(snapshot, execution=corrupted_execution)

    target, executions = _coordinator()
    target.submit(plan)
    before = target.snapshot()

    with pytest.raises(DeploymentWaveError, match="execution snapshot structure"):
        target.restore(corrupted)
    assert target.snapshot() == before
    assert executions.records["operation-api"].state is OperationState.PENDING


def test_restore_rejects_execution_record_with_missing_spec_before_traversal() -> None:
    plan = DeploymentWavePlan("plan", "social", (_execution("api"),))
    source, _ = _coordinator()
    source.submit(plan)
    snapshot = source.snapshot()
    execution_record = replace(snapshot.execution.records[0], spec=None)
    corrupted_execution = replace(snapshot.execution, records=(execution_record,))
    corrupted = replace(snapshot, execution=corrupted_execution)

    target, executions = _coordinator()
    target.submit(plan)
    before = target.snapshot()

    with pytest.raises(DeploymentWaveError, match="execution snapshot structure"):
        target.restore(corrupted)
    assert target.snapshot() == before
    assert executions.records["operation-api"].state is OperationState.PENDING


def test_submit_revalidates_full_service_collection_before_nested_publication() -> None:
    coordinator, executions = _coordinator()
    first = _execution("api")
    second = _execution("worker")
    plan = DeploymentWavePlan("plan", "social", (first, second))
    object.__setattr__(plan, "services", (first, object()))

    with pytest.raises(DeploymentWaveError, match="invalid rollout plan"):
        coordinator.submit(plan)

    assert executions.records == {}
    assert coordinator.snapshot().plans == ()


def test_submit_revalidates_nested_execution_before_any_service_publication() -> None:
    coordinator, executions = _coordinator()
    first = _execution("api")
    second = _execution("worker")
    plan = DeploymentWavePlan("plan", "social", (first, second))
    object.__setattr__(second.execution, "request", object())

    with pytest.raises(DeploymentWaveError, match="invalid rollout plan"):
        coordinator.submit(plan)

    assert executions.records == {}
    assert coordinator.snapshot().plans == ()


def test_submit_revalidates_postconstruction_wave_before_publication() -> None:
    coordinator, executions = _coordinator()
    first = _execution("api")
    second = _execution("worker", wave=1)
    plan = DeploymentWavePlan("plan", "social", (first, second))
    object.__setattr__(second, "wave", True)

    with pytest.raises(DeploymentWaveError, match="invalid rollout plan"):
        coordinator.submit(plan)

    assert executions.records == {}
    assert coordinator.snapshot().plans == ()


def test_submit_preflights_late_execution_conflict_before_any_publication() -> None:
    coordinator, executions = _coordinator()
    incumbent = _execution("worker")
    conflicting = replace(
        incumbent.execution,
        credential_scope="different-scope",
    )
    executions.submit(conflicting)
    before = coordinator.snapshot()

    plan = DeploymentWavePlan(
        "plan",
        "social",
        (_execution("api"), incumbent),
    )

    with pytest.raises(DeploymentWaveError, match="conflicts with prior payload"):
        coordinator.submit(plan)

    assert coordinator.snapshot() == before
    assert "operation-api" not in executions.records
