from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

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
from nika_core.product_factory_deployment_promotions import (
    DeploymentPromotionCoordinator,
    DeploymentPromotionError,
    DeploymentPromotionPlan,
    DeploymentPromotionSnapshot,
    ProductionPromotionAuthorization,
    ServicePromotionSpec,
    build_production_promotion_action,
    build_promotion_wave_plan,
)
from nika_core.security.policy import ApprovalEvidence
from nika_core.product_factory_deployment_waves import (
    DeploymentWaveCoordinator,
    DeploymentWaveSnapshot,
    RolloutState,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _execution(
    service: str,
    tier: EnvironmentTier,
    *,
    project: str = "social",
    source_sha: str = SHA_A,
    digest: str = DIGEST_A,
) -> DeploymentExecutionSpec:
    request = ExecutionRequest(
        project_id=project,
        work_id=f"work-{service}-{tier.value}",
        platform=Platform.LINUX,
        required_features=frozenset({"deployment"}),
        required_toolchains=frozenset(),
        resources=ResourceEnvelope(1, 256, 512),
    )
    environment = EnvironmentIdentity(
        environment_id=f"{tier.value}-shared",
        project_id=project,
        tier=tier,
        provider_ref=f"provider:{tier.value}",
    )
    release = ReleaseRef(project, "1.0.0", source_sha, digest)
    intent = DeploymentIntent(
        f"intent-{service}-{tier.value}",
        project,
        environment,
        release,
    )
    return DeploymentExecutionSpec(
        operation_id=f"operation-{service}-{tier.value}",
        request=request,
        intent=intent,
        credential_ref=f"credential:{tier.value}",
        credential_audience="provider",
        credential_scope="deploy",
    )


def _service(
    service: str,
    *,
    batch: int = 0,
    depends_on: tuple[str, ...] = (),
    source_sha: str = SHA_A,
    digest: str = DIGEST_A,
) -> ServicePromotionSpec:
    return ServicePromotionSpec(
        service,
        batch,
        _execution(
            service,
            EnvironmentTier.STAGING,
            source_sha=source_sha,
            digest=digest,
        ),
        _execution(
            service,
            EnvironmentTier.PRODUCTION,
            source_sha=source_sha,
            digest=digest,
        ),
        depends_on,
    )


def _authorization(
    plan: DeploymentPromotionPlan,
    service_id: str,
    *,
    authorization_id: str | None = None,
    approved_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ProductionPromotionAuthorization:
    action = build_production_promotion_action(plan, service_id)
    approval = ApprovalEvidence(
        approval_id=(
            authorization_id
            or f"approval-{plan.plan_id}-{service_id}"
        ),
        action_fingerprint=action.approval_fingerprint,
        approved_at=(
            approved_at
            or datetime(2020, 1, 1, tzinfo=UTC)
        ),
        expires_at=(
            expires_at
            or datetime(2099, 1, 1, tzinfo=UTC)
        ),
    )
    return ProductionPromotionAuthorization(
        plan_id=plan.plan_id,
        service_id=service_id,
        approval=approval,
    )


class _FakeExecutions:
    def __init__(self) -> None:
        self.records: dict[str, DeploymentExecutionRecord] = {}
        self.complete_state: dict[str, OperationState] = {}
        self.prepare_state: dict[str, OperationState] = {}
        self.reconcile_state: dict[str, OperationState] = {}
        self.complete_calls: list[str] = []

    def submit(
        self,
        spec: DeploymentExecutionSpec,
    ) -> DeploymentExecutionRecord:
        return self.records.setdefault(
            spec.operation_id,
            DeploymentExecutionRecord(
                spec,
                OperationState.PENDING,
            ),
        )

    def get(
        self,
        operation_id: str,
    ) -> DeploymentExecutionRecord:
        return self.records[operation_id]

    def prepare(
        self,
        operation_id: str,
    ) -> DeploymentExecutionRecord:
        record = self.records[operation_id]
        state = self.prepare_state.get(
            operation_id,
            OperationState.PREPARED,
        )
        updated = replace(
            record,
            state=state,
            attempt=record.attempt + 1,
        )
        self.records[operation_id] = updated
        return updated

    def retry(
        self,
        operation_id: str,
    ) -> DeploymentExecutionRecord:
        return self.prepare(operation_id)

    def complete(
        self,
        operation_id: str,
    ) -> DeploymentExecutionRecord:
        self.complete_calls.append(operation_id)
        record = self.records[operation_id]
        state = self.complete_state.get(
            operation_id,
            OperationState.SUCCEEDED,
        )
        deployment_state = {
            OperationState.SUCCEEDED: DeploymentState.HEALTHY,
            OperationState.REJECTED: DeploymentState.REJECTED,
            OperationState.ROLLED_BACK: DeploymentState.ROLLED_BACK,
            OperationState.RECONCILE_REQUIRED: DeploymentState.UNCERTAIN,
        }.get(state)
        updated = replace(
            record,
            state=state,
            deployment_state=deployment_state,
        )
        self.records[operation_id] = updated
        return updated

    def reconcile(
        self,
        operation_id: str,
    ) -> DeploymentExecutionRecord:
        record = self.records[operation_id]
        state = self.reconcile_state.get(
            operation_id,
            OperationState.SUCCEEDED,
        )
        updated = replace(record, state=state)
        self.records[operation_id] = updated
        return updated

    def snapshot(self) -> DeploymentExecutionSnapshot:
        records = []
        for operation_id in sorted(self.records):
            record = self.records[operation_id]
            state = (
                OperationState.RECOVERY_REQUIRED
                if record.state is OperationState.PREPARED
                else record.state
            )
            records.append(
                replace(
                    record,
                    state=state,
                    node_id=None,
                )
            )
        return DeploymentExecutionSnapshot(tuple(records))

    def restore(
        self,
        snapshot: DeploymentExecutionSnapshot,
    ) -> None:
        self.records = {
            record.spec.operation_id: record
            for record in snapshot.records
        }


def _coordinator() -> tuple[
    DeploymentPromotionCoordinator,
    _FakeExecutions,
]:
    executions = _FakeExecutions()
    waves = DeploymentWaveCoordinator(executions)  # type: ignore[arg-type]
    return DeploymentPromotionCoordinator(waves), executions


def test_service_requires_exact_release_identity_across_environments() -> None:
    staging = _execution("api", EnvironmentTier.STAGING)
    different_sha = _execution(
        "api",
        EnvironmentTier.PRODUCTION,
        source_sha=SHA_B,
    )
    with pytest.raises(DeploymentPromotionError):
        ServicePromotionSpec(
            "api",
            0,
            staging,
            different_sha,
        )

    different_digest = _execution(
        "api",
        EnvironmentTier.PRODUCTION,
        digest=DIGEST_B,
    )
    with pytest.raises(DeploymentPromotionError):
        ServicePromotionSpec(
            "api",
            0,
            staging,
            different_digest,
        )


def test_service_requires_staging_then_production_and_distinct_identity() -> None:
    staging = _execution("api", EnvironmentTier.STAGING)
    production = _execution("api", EnvironmentTier.PRODUCTION)

    with pytest.raises(DeploymentPromotionError):
        ServicePromotionSpec(
            "api",
            0,
            production,
            staging,
        )

    duplicate_operation = replace(
        production,
        operation_id=staging.operation_id,
    )
    with pytest.raises(DeploymentPromotionError):
        ServicePromotionSpec(
            "api",
            0,
            staging,
            duplicate_operation,
        )


def test_plan_requires_dependencies_to_complete_in_earlier_batch() -> None:
    api = _service("api", batch=0)
    feed = _service(
        "feed",
        batch=0,
        depends_on=("api",),
    )
    with pytest.raises(DeploymentPromotionError):
        DeploymentPromotionPlan(
            "plan",
            "social",
            (api, feed),
        )

    unknown = _service(
        "feed",
        batch=1,
        depends_on=("missing",),
    )
    with pytest.raises(DeploymentPromotionError):
        DeploymentPromotionPlan(
            "plan",
            "social",
            (api, unknown),
        )


def test_wave_plan_encodes_exact_stage_and_cross_service_dependencies() -> None:
    api = _service("api", batch=0)
    feed = _service(
        "feed",
        batch=1,
        depends_on=("api",),
    )
    wave = build_promotion_wave_plan(
        DeploymentPromotionPlan(
            "plan",
            "social",
            (feed, api),
        )
    )
    by_id = {
        service.service_id: service
        for service in wave.services
    }

    assert by_id["api@staging"].wave == 0
    assert by_id["api@production"].wave == 1
    assert by_id["api@production"].depends_on == ("api@staging",)
    assert by_id["feed@staging"].wave == 2
    assert by_id["feed@staging"].depends_on == ("api@production",)
    assert by_id["feed@production"].wave == 3
    assert by_id["feed@production"].depends_on == ("feed@staging",)


def test_production_waits_for_explicit_exact_authorization() -> None:
    coordinator, executions = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    submitted = coordinator.submit(plan)
    assert submitted.state is RolloutState.PENDING

    staged = coordinator.advance("plan")
    assert staged.services[0].staging.state is OperationState.SUCCEEDED
    assert staged.services[0].production.state is OperationState.PENDING
    assert staged.production_authorization_required == ("api",)
    assert executions.complete_calls == ["operation-api-staging"]

    blocked = coordinator.advance("plan")
    assert blocked.services[0].production.state is OperationState.PENDING
    assert blocked.production_authorization_required == ("api",)
    assert executions.complete_calls == ["operation-api-staging"]

    coordinator.authorize_production(_authorization(plan, "api"))
    production = coordinator.advance("plan")
    assert production.state is RolloutState.SUCCEEDED
    assert production.production_authorization_required == ()
    assert production.services[0].production.state is OperationState.SUCCEEDED
    assert executions.complete_calls == [
        "operation-api-staging",
        "operation-api-production",
    ]


def test_authorization_is_rejected_before_healthy_staging() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    coordinator.submit(plan)

    with pytest.raises(
        DeploymentPromotionError,
        match="healthy staging",
    ):
        coordinator.authorize_production(
            _authorization(plan, "api")
        )


def test_authorization_reuses_canonical_exact_action_and_expiry() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    coordinator.submit(plan)
    coordinator.advance("plan")
    exact = _authorization(plan, "api")

    action = build_production_promotion_action(plan, "api")
    assert action.approval_required is True
    assert action.risk.value == "high_impact"

    with pytest.raises(
        DeploymentPromotionError,
        match="exact promotion action",
    ):
        coordinator.authorize_production(
            replace(
                exact,
                approval=replace(
                    exact.approval,
                    action_fingerprint="0" * 64,
                ),
            ),
            now=NOW,
        )

    expired = _authorization(
        plan,
        "api",
        authorization_id="approval-expired",
        approved_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1),
    )
    with pytest.raises(
        DeploymentPromotionError,
        match="not currently valid",
    ):
        coordinator.authorize_production(
            expired,
            now=NOW,
        )

    alternate = DeploymentPromotionPlan(
        "other-plan",
        "social",
        (
            _service(
                "api",
                source_sha=SHA_B,
                digest=DIGEST_B,
            ),
        ),
    )
    alternate_action = build_production_promotion_action(
        alternate,
        "api",
    )
    assert (
        alternate_action.approval_fingerprint
        != action.approval_fingerprint
    )


def test_authorization_is_idempotent_but_conflicts_fail_closed() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (
            _service("api"),
            _service("worker"),
        ),
    )
    coordinator.submit(plan)
    coordinator.advance("plan")

    api = _authorization(
        plan,
        "api",
        authorization_id="approval-shared-id",
    )
    assert coordinator.authorize_production(api) == api
    assert coordinator.authorize_production(api) == api

    with pytest.raises(
        DeploymentPromotionError,
        match="conflicts",
    ):
        coordinator.authorize_production(
            replace(
                api,
                approval=replace(
                    api.approval,
                    approval_id="approval-conflicting-payload",
                ),
            )
        )

    with pytest.raises(
        DeploymentPromotionError,
        match="already belongs",
    ):
        coordinator.authorize_production(
            _authorization(
                plan,
                "worker",
                authorization_id="approval-shared-id",
            )
        )


def test_failed_staging_does_not_block_authorized_parallel_service() -> None:
    coordinator, executions = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (
            _service("profiles"),
            _service("messages"),
        ),
    )
    coordinator.submit(plan)
    executions.complete_state[
        "operation-messages-staging"
    ] = OperationState.ROLLED_BACK

    staged = coordinator.advance("plan")
    by_id = {
        service.service_id: service
        for service in staged.services
    }
    assert by_id["profiles"].staging.state is OperationState.SUCCEEDED
    assert by_id["messages"].staging.state is OperationState.ROLLED_BACK
    assert staged.state is RolloutState.PARTIAL_FAILURE
    assert staged.production_authorization_required == ("profiles",)

    coordinator.authorize_production(
        _authorization(plan, "profiles")
    )
    production = coordinator.advance("plan")
    by_id = {
        service.service_id: service
        for service in production.services
    }
    assert by_id["profiles"].production.state is OperationState.SUCCEEDED
    assert by_id["messages"].production.state is OperationState.PENDING
    assert "operation-messages-production" not in executions.complete_calls


def test_credential_block_is_retried_without_skipping_authorization_gate() -> None:
    coordinator, executions = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (
            _service("search"),
            _service("profiles"),
        ),
    )
    coordinator.submit(plan)
    executions.prepare_state[
        "operation-search-staging"
    ] = OperationState.BLOCKED_CREDENTIAL

    blocked = coordinator.advance("plan")
    by_id = {
        service.service_id: service
        for service in blocked.services
    }
    assert by_id["search"].staging.state is OperationState.BLOCKED_CREDENTIAL
    assert by_id["profiles"].staging.state is OperationState.SUCCEEDED
    assert blocked.state is RolloutState.PAUSED
    assert blocked.production_authorization_required == ()
    assert all(
        "production" not in operation
        for operation in executions.complete_calls
    )

    executions.prepare_state[
        "operation-search-staging"
    ] = OperationState.PREPARED
    recovered = coordinator.advance("plan")
    by_id = {
        service.service_id: service
        for service in recovered.services
    }
    assert by_id["search"].staging.state is OperationState.SUCCEEDED
    assert by_id["profiles"].production.state is OperationState.PENDING
    assert recovered.production_authorization_required == (
        "profiles",
        "search",
    )

    coordinator.authorize_production(
        _authorization(plan, "profiles")
    )
    still_blocked = coordinator.advance("plan")
    assert still_blocked.production_authorization_required == ("search",)
    assert all(
        "production" not in operation
        for operation in executions.complete_calls
    )

    coordinator.authorize_production(
        _authorization(plan, "search")
    )
    promoted = coordinator.advance("plan")
    assert promoted.state is RolloutState.SUCCEEDED


def test_uncertain_production_reconciles_without_duplicate_completion() -> None:
    coordinator, executions = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("media"),),
    )
    coordinator.submit(plan)
    coordinator.advance("plan")
    coordinator.authorize_production(
        _authorization(plan, "media")
    )
    executions.complete_state[
        "operation-media-production"
    ] = OperationState.RECONCILE_REQUIRED
    executions.reconcile_state[
        "operation-media-production"
    ] = OperationState.SUCCEEDED

    result = coordinator.advance("plan")
    assert result.state is RolloutState.SUCCEEDED
    assert (
        executions.complete_calls.count(
            "operation-media-production"
        )
        == 1
    )


def test_snapshot_restart_preserves_authorization_and_exact_release() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    coordinator.submit(plan)
    coordinator.advance("plan")
    authorization = _authorization(plan, "api")
    coordinator.authorize_production(authorization)
    snapshot = coordinator.snapshot()

    assert snapshot.authorizations == (authorization,)

    restored, restored_executions = _coordinator()
    restored.restore(snapshot)
    before = restored.get("plan")
    assert before.services[0].release_sha == SHA_A
    assert before.services[0].artifact_digest == DIGEST_A
    assert before.services[0].staging.state is OperationState.SUCCEEDED
    assert before.services[0].production.state is OperationState.PENDING
    assert before.production_authorization_required == ()

    after = restored.advance("plan")
    assert after.state is RolloutState.SUCCEEDED
    assert restored_executions.complete_calls == [
        "operation-api-production"
    ]


def test_snapshot_without_authorization_remains_blocked_after_restart() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    coordinator.submit(plan)
    staged = coordinator.advance("plan")
    assert staged.production_authorization_required == ("api",)
    snapshot = coordinator.snapshot()
    assert snapshot.authorizations == ()

    restored, restored_executions = _coordinator()
    restored.restore(snapshot)
    blocked = restored.advance("plan")
    assert blocked.production_authorization_required == ("api",)
    assert blocked.services[0].production.state is OperationState.PENDING
    assert restored_executions.complete_calls == []


def test_restore_rejects_production_progress_without_durable_authorization() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    coordinator.submit(plan)
    coordinator.advance("plan")
    coordinator.authorize_production(
        _authorization(plan, "api")
    )
    completed = coordinator.advance("plan")
    assert completed.state is RolloutState.SUCCEEDED
    snapshot = coordinator.snapshot()

    corrupted = DeploymentPromotionSnapshot(
        snapshot.plans,
        snapshot.waves,
        (),
    )
    restored, _ = _coordinator()
    with pytest.raises(
        DeploymentPromotionError,
        match="not backed by durable authorization",
    ):
        restored.restore(corrupted)


def test_restore_rejects_authorization_not_backed_by_staging() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    corrupted = DeploymentPromotionSnapshot(
        snapshot.plans,
        snapshot.waves,
        (_authorization(plan, "api"),),
    )

    restored, _ = _coordinator()
    with pytest.raises(
        DeploymentPromotionError,
        match="not backed by healthy staging",
    ):
        restored.restore(corrupted)


def test_restore_rejects_plan_that_disagrees_with_wave_snapshot() -> None:
    coordinator, _ = _coordinator()
    plan = DeploymentPromotionPlan(
        "plan",
        "social",
        (_service("api"),),
    )
    coordinator.submit(plan)
    snapshot = coordinator.snapshot()
    wave_record = snapshot.waves.plans[0]
    corrupted_wave_plan = replace(
        wave_record.plan,
        plan_id="promotion:other",
    )
    corrupted_wave_record = replace(
        wave_record,
        plan=corrupted_wave_plan,
    )
    corrupted = DeploymentPromotionSnapshot(
        snapshot.plans,
        DeploymentWaveSnapshot(
            (corrupted_wave_record,),
            snapshot.waves.execution,
        ),
        snapshot.authorizations,
    )

    restored, _ = _coordinator()
    with pytest.raises(DeploymentPromotionError):
        restored.restore(corrupted)


def test_sixty_service_three_batch_authorized_restart_is_deterministic() -> None:
    coordinator, _ = _coordinator()
    services = []
    for index in range(60):
        batch = index // 20
        depends_on = (
            (f"service-{index - 20:02d}",)
            if batch
            else ()
        )
        services.append(
            _service(
                f"service-{index:02d}",
                batch=batch,
                depends_on=depends_on,
            )
        )
    plan = DeploymentPromotionPlan(
        "scale-plan",
        "social",
        tuple(services),
    )
    coordinator.submit(plan)

    for batch in range(2):
        staged = coordinator.advance("scale-plan")
        expected = tuple(
            f"service-{index:02d}"
            for index in range(batch * 20, (batch + 1) * 20)
        )
        assert staged.production_authorization_required == expected
        for service_id in expected:
            coordinator.authorize_production(
                _authorization(plan, service_id)
            )
        promoted = coordinator.advance("scale-plan")
        assert all(
            service.production.state is OperationState.SUCCEEDED
            for service in promoted.services
            if service.service_id in expected
        )

    snapshot = coordinator.snapshot()
    restored, _ = _coordinator()
    restored.restore(snapshot)

    staged = restored.advance("scale-plan")
    expected = tuple(
        f"service-{index:02d}"
        for index in range(40, 60)
    )
    assert staged.production_authorization_required == expected
    for service_id in expected:
        restored.authorize_production(
            _authorization(plan, service_id)
        )
    final = restored.advance("scale-plan")

    assert final.state is RolloutState.SUCCEEDED
    assert len(final.services) == 60
    assert all(
        service.release_sha == SHA_A
        for service in final.services
    )
    assert all(
        service.artifact_digest == DIGEST_A
        for service in final.services
    )
    assert all(
        service.staging.state is OperationState.SUCCEEDED
        and service.production.state is OperationState.SUCCEEDED
        for service in final.services
    )
    assert len(restored.snapshot().authorizations) == 60
