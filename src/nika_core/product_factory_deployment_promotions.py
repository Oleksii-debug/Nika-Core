from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from nika_core.product_factory_deployment import EnvironmentTier, ReleaseRef
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionSpec,
    OperationState,
)
from nika_core.product_factory_deployment_waves import (
    DeploymentWaveCoordinator,
    DeploymentWavePlan,
    DeploymentWaveRecord,
    DeploymentWaveSnapshot,
    RolloutState,
    ServiceRolloutRecord,
    ServiceRolloutSpec,
)
from nika_core.security.policy import (
    ActionIntent,
    ApprovalEvidence,
    ApprovalLedger,
)
from nika_core.tools import ToolRisk


class DeploymentPromotionError(ValueError):
    """Raised when PF3 multi-environment promotion invariants are violated."""


@dataclass(frozen=True, slots=True)
class ProductionPromotionAuthorization:
    """Durable acceptance of one canonical high-impact approval."""

    plan_id: str
    service_id: str
    approval: ApprovalEvidence

    def __post_init__(self) -> None:
        if not self.plan_id.strip() or not self.service_id.strip():
            raise DeploymentPromotionError(
                "production authorization identity fields must not be empty"
            )

    @property
    def authorization_id(self) -> str:
        return self.approval.approval_id


@dataclass(frozen=True, slots=True)
class ServicePromotionSpec:
    service_id: str
    batch: int
    staging: DeploymentExecutionSpec
    production: DeploymentExecutionSpec
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.service_id.strip():
            raise DeploymentPromotionError(
                "promotion service identity must not be empty"
            )
        if self.batch < 0:
            raise DeploymentPromotionError("promotion batch must not be negative")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise DeploymentPromotionError(
                "promotion dependencies must not contain duplicates"
            )
        if self.service_id in self.depends_on:
            raise DeploymentPromotionError(
                "promotion service must not depend on itself"
            )
        if self.staging.intent.environment.tier is not EnvironmentTier.STAGING:
            raise DeploymentPromotionError(
                "staging execution must target a staging environment"
            )
        if self.production.intent.environment.tier is not EnvironmentTier.PRODUCTION:
            raise DeploymentPromotionError(
                "production execution must target a production environment"
            )
        if (
            self.staging.intent.environment.environment_id
            == self.production.intent.environment.environment_id
        ):
            raise DeploymentPromotionError(
                "staging and production environments must be distinct"
            )
        if self.staging.operation_id == self.production.operation_id:
            raise DeploymentPromotionError(
                "promotion stages require distinct operation identities"
            )
        if self.staging.intent.intent_id == self.production.intent.intent_id:
            raise DeploymentPromotionError(
                "promotion stages require distinct deployment intent identities"
            )
        if self.staging.intent.release != self.production.intent.release:
            raise DeploymentPromotionError(
                "production promotion must use the exact staging release identity"
            )

    @property
    def release(self) -> ReleaseRef:
        return self.staging.intent.release


@dataclass(frozen=True, slots=True)
class DeploymentPromotionPlan:
    plan_id: str
    project_id: str
    services: tuple[ServicePromotionSpec, ...]

    def __post_init__(self) -> None:
        if not self.plan_id.strip() or not self.project_id.strip():
            raise DeploymentPromotionError("promotion plan identity must not be empty")
        if not self.services:
            raise DeploymentPromotionError(
                "promotion plan must contain at least one service"
            )

        service_ids = [service.service_id for service in self.services]
        if len(service_ids) != len(set(service_ids)):
            raise DeploymentPromotionError(
                "promotion plan contains duplicate service identities"
            )
        by_id = {service.service_id: service for service in self.services}

        operation_ids: list[str] = []
        intent_ids: list[str] = []
        for service in self.services:
            if (
                service.staging.intent.project_id != self.project_id
                or service.production.intent.project_id != self.project_id
            ):
                raise DeploymentPromotionError(
                    "promotion execution belongs to another project"
                )
            operation_ids.extend(
                (service.staging.operation_id, service.production.operation_id)
            )
            intent_ids.extend(
                (service.staging.intent.intent_id, service.production.intent.intent_id)
            )
            unknown = set(service.depends_on) - by_id.keys()
            if unknown:
                raise DeploymentPromotionError(
                    "promotion dependency references an unknown service"
                )
            if any(
                by_id[parent].batch >= service.batch
                for parent in service.depends_on
            ):
                raise DeploymentPromotionError(
                    "promotion dependencies must complete in an earlier batch"
                )

        if len(operation_ids) != len(set(operation_ids)):
            raise DeploymentPromotionError(
                "promotion plan contains duplicate execution operation identities"
            )
        if len(intent_ids) != len(set(intent_ids)):
            raise DeploymentPromotionError(
                "promotion plan contains duplicate deployment intent identities"
            )


@dataclass(frozen=True, slots=True)
class PromotionStageRecord:
    tier: EnvironmentTier
    environment_id: str
    operation_id: str
    state: OperationState
    attempt: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServicePromotionRecord:
    service_id: str
    release_sha: str
    artifact_digest: str
    staging: PromotionStageRecord
    production: PromotionStageRecord


@dataclass(frozen=True, slots=True)
class DeploymentPromotionRecord:
    plan: DeploymentPromotionPlan
    state: RolloutState
    services: tuple[ServicePromotionRecord, ...]
    production_authorization_required: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentPromotionSnapshot:
    plans: tuple[DeploymentPromotionPlan, ...]
    waves: DeploymentWaveSnapshot
    authorizations: tuple[ProductionPromotionAuthorization, ...] = ()


@dataclass(slots=True)
class DeploymentPromotionCoordinator:
    waves: DeploymentWaveCoordinator
    approvals: ApprovalLedger = field(default_factory=ApprovalLedger, repr=False)
    _plans: dict[str, DeploymentPromotionPlan] = field(
        default_factory=dict, init=False, repr=False
    )
    _authorizations: dict[
        tuple[str, str], ProductionPromotionAuthorization
    ] = field(default_factory=dict, init=False, repr=False)

    def submit(self, plan: DeploymentPromotionPlan) -> DeploymentPromotionRecord:
        existing = self._plans.get(plan.plan_id)
        if existing is not None:
            if existing != plan:
                raise DeploymentPromotionError(
                    "promotion plan id conflicts with prior payload"
                )
            return self._summarize(
                existing,
                self.waves.get(_wave_plan_id(plan.plan_id)),
            )

        wave_plan = build_promotion_wave_plan(plan)
        wave_record = self.waves.submit(wave_plan)
        self._plans[plan.plan_id] = plan
        return self._summarize(plan, wave_record)

    def authorize_production(
        self,
        authorization: ProductionPromotionAuthorization,
        *,
        now: datetime | None = None,
    ) -> ProductionPromotionAuthorization:
        plan = self._plan(authorization.plan_id)
        service = self._service(plan, authorization.service_id)
        self._validate_authorization_binding(authorization, plan, service)

        wave_record = self.waves.get(_wave_plan_id(plan.plan_id))
        staging = self._wave_service(
            wave_record,
            _stage_service_id(service.service_id, EnvironmentTier.STAGING),
        )
        if staging.state is not OperationState.SUCCEEDED:
            raise DeploymentPromotionError(
                "production authorization requires completed healthy staging"
            )

        key = (authorization.plan_id, authorization.service_id)
        existing = self._authorizations.get(key)
        if existing is not None:
            if existing != authorization:
                raise DeploymentPromotionError(
                    "production authorization conflicts with prior payload"
                )
            return existing

        for accepted in self._authorizations.values():
            if accepted.authorization_id == authorization.authorization_id:
                raise DeploymentPromotionError(
                    "production authorization id already belongs to another promotion"
                )

        intent = build_production_promotion_action(
            plan,
            authorization.service_id,
        )
        try:
            self.approvals.consume(
                intent,
                authorization.approval,
                now=now,
            )
        except (PermissionError, ValueError) as exc:
            raise DeploymentPromotionError(
                f"production authorization rejected: {exc}"
            ) from exc

        self._authorizations[key] = authorization
        return authorization

    def advance(self, plan_id: str) -> DeploymentPromotionRecord:
        plan = self._plan(plan_id)
        wave_record = self.waves.get(_wave_plan_id(plan_id))
        required = self._required_production_authorizations(plan, wave_record)
        if required:
            return self._summarize(plan, wave_record)
        wave_record = self.waves.advance(_wave_plan_id(plan_id))
        return self._summarize(plan, wave_record)

    def get(self, plan_id: str) -> DeploymentPromotionRecord:
        plan = self._plan(plan_id)
        return self._summarize(
            plan,
            self.waves.get(_wave_plan_id(plan_id)),
        )

    def snapshot(self) -> DeploymentPromotionSnapshot:
        return DeploymentPromotionSnapshot(
            tuple(self._plans[key] for key in sorted(self._plans)),
            self.waves.snapshot(),
            tuple(
                self._authorizations[key]
                for key in sorted(self._authorizations)
            ),
        )

    def restore(self, snapshot: DeploymentPromotionSnapshot) -> None:
        plan_ids = [plan.plan_id for plan in snapshot.plans]
        if len(plan_ids) != len(set(plan_ids)):
            raise DeploymentPromotionError(
                "promotion snapshot contains duplicate plans"
            )

        expected = {
            _wave_plan_id(plan.plan_id): build_promotion_wave_plan(plan)
            for plan in snapshot.plans
        }
        actual = {
            record.plan.plan_id: record.plan
            for record in snapshot.waves.plans
        }
        if actual != expected:
            raise DeploymentPromotionError(
                "promotion snapshot wave plans do not match durable promotion plans"
            )

        plans = {plan.plan_id: plan for plan in snapshot.plans}
        wave_records = {
            record.plan.plan_id: record
            for record in snapshot.waves.plans
        }
        authorizations = self._validated_snapshot_authorizations(
            snapshot.authorizations,
            plans,
            wave_records,
        )
        self._validate_production_progress_authorized(
            plans,
            wave_records,
            authorizations,
        )

        self.waves.restore(snapshot.waves)
        self._plans = plans
        self._authorizations = authorizations

    def _validated_snapshot_authorizations(
        self,
        snapshot_authorizations: tuple[
            ProductionPromotionAuthorization, ...
        ],
        plans: dict[str, DeploymentPromotionPlan],
        wave_records: dict[str, DeploymentWaveRecord],
    ) -> dict[tuple[str, str], ProductionPromotionAuthorization]:
        restored: dict[
            tuple[str, str], ProductionPromotionAuthorization
        ] = {}
        authorization_ids: set[str] = set()
        for authorization in snapshot_authorizations:
            if authorization.authorization_id in authorization_ids:
                raise DeploymentPromotionError(
                    "promotion snapshot contains duplicate authorization identities"
                )
            authorization_ids.add(authorization.authorization_id)

            plan = plans.get(authorization.plan_id)
            if plan is None:
                raise DeploymentPromotionError(
                    "production authorization references an unknown promotion plan"
                )
            service = self._service(plan, authorization.service_id)
            self._validate_authorization_binding(
                authorization,
                plan,
                service,
            )
            wave_record = wave_records[_wave_plan_id(plan.plan_id)]
            staging = self._wave_service(
                wave_record,
                _stage_service_id(
                    service.service_id,
                    EnvironmentTier.STAGING,
                ),
            )
            if staging.state is not OperationState.SUCCEEDED:
                raise DeploymentPromotionError(
                    "production authorization is not backed by healthy staging"
                )

            key = (authorization.plan_id, authorization.service_id)
            if key in restored:
                raise DeploymentPromotionError(
                    "promotion snapshot contains duplicate service authorizations"
                )
            restored[key] = authorization
        return restored

    def _validate_production_progress_authorized(
        self,
        plans: dict[str, DeploymentPromotionPlan],
        wave_records: dict[str, DeploymentWaveRecord],
        authorizations: dict[
            tuple[str, str], ProductionPromotionAuthorization
        ],
    ) -> None:
        for plan in plans.values():
            wave_record = wave_records[_wave_plan_id(plan.plan_id)]
            for service in plan.services:
                production = self._wave_service(
                    wave_record,
                    _stage_service_id(
                        service.service_id,
                        EnvironmentTier.PRODUCTION,
                    ),
                )
                progressed = (
                    production.state is not OperationState.PENDING
                    or production.attempt != 0
                    or bool(production.evidence_refs)
                )
                if progressed and (
                    plan.plan_id,
                    service.service_id,
                ) not in authorizations:
                    raise DeploymentPromotionError(
                        "production progress is not backed by durable authorization"
                    )

    def _required_production_authorizations(
        self,
        plan: DeploymentPromotionPlan,
        wave_record: DeploymentWaveRecord,
    ) -> tuple[str, ...]:
        incomplete_waves = sorted(
            {
                record.wave
                for record in wave_record.services
                if record.state not in _TERMINAL_STATES
            }
        )
        if not incomplete_waves:
            return ()

        active_wave = incomplete_waves[0]
        wave_specs = {
            service.service_id: service
            for service in wave_record.plan.services
        }
        wave_states = {
            service.service_id: service
            for service in wave_record.services
        }
        required: list[str] = []
        for service in sorted(
            plan.services,
            key=lambda item: (item.batch, item.service_id),
        ):
            production_id = _stage_service_id(
                service.service_id,
                EnvironmentTier.PRODUCTION,
            )
            spec = wave_specs[production_id]
            record = wave_states[production_id]
            if spec.wave != active_wave or record.state in _TERMINAL_STATES:
                continue
            if not all(
                wave_states[parent].state is OperationState.SUCCEEDED
                for parent in spec.depends_on
            ):
                continue
            if (plan.plan_id, service.service_id) not in self._authorizations:
                required.append(service.service_id)
        return tuple(required)

    @staticmethod
    def _validate_authorization_binding(
        authorization: ProductionPromotionAuthorization,
        plan: DeploymentPromotionPlan,
        service: ServicePromotionSpec,
    ) -> None:
        if authorization.plan_id != plan.plan_id:
            raise DeploymentPromotionError(
                "production authorization plan identity mismatch"
            )
        if authorization.service_id != service.service_id:
            raise DeploymentPromotionError(
                "production authorization service identity mismatch"
            )
        intent = build_production_promotion_action(
            plan,
            service.service_id,
        )
        if authorization.approval.action_fingerprint != intent.approval_fingerprint:
            raise DeploymentPromotionError(
                "production authorization does not match exact promotion action"
            )

    @staticmethod
    def _wave_service(
        wave_record: DeploymentWaveRecord,
        service_id: str,
    ) -> ServiceRolloutRecord:
        for service in wave_record.services:
            if service.service_id == service_id:
                return service
        raise DeploymentPromotionError(
            "promotion wave is missing expected service stage"
        )

    def _plan(self, plan_id: str) -> DeploymentPromotionPlan:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise DeploymentPromotionError("unknown promotion plan")
        return plan

    @staticmethod
    def _service(
        plan: DeploymentPromotionPlan,
        service_id: str,
    ) -> ServicePromotionSpec:
        for service in plan.services:
            if service.service_id == service_id:
                return service
        raise DeploymentPromotionError(
            "production authorization references an unknown promotion service"
        )

    def _summarize(
        self,
        plan: DeploymentPromotionPlan,
        wave_record: DeploymentWaveRecord,
    ) -> DeploymentPromotionRecord:
        records = {
            record.service_id: record
            for record in wave_record.services
        }
        services: list[ServicePromotionRecord] = []
        for service in sorted(
            plan.services,
            key=lambda item: (item.batch, item.service_id),
        ):
            staging = records[
                _stage_service_id(
                    service.service_id,
                    EnvironmentTier.STAGING,
                )
            ]
            production = records[
                _stage_service_id(
                    service.service_id,
                    EnvironmentTier.PRODUCTION,
                )
            ]
            services.append(
                ServicePromotionRecord(
                    service.service_id,
                    service.release.source_sha,
                    service.release.artifact_digest,
                    PromotionStageRecord(
                        EnvironmentTier.STAGING,
                        service.staging.intent.environment.environment_id,
                        staging.operation_id,
                        staging.state,
                        staging.attempt,
                        staging.evidence_refs,
                    ),
                    PromotionStageRecord(
                        EnvironmentTier.PRODUCTION,
                        service.production.intent.environment.environment_id,
                        production.operation_id,
                        production.state,
                        production.attempt,
                        production.evidence_refs,
                    ),
                )
            )
        return DeploymentPromotionRecord(
            plan,
            wave_record.state,
            tuple(services),
            self._required_production_authorizations(
                plan,
                wave_record,
            ),
        )


def build_production_promotion_action(
    plan: DeploymentPromotionPlan,
    service_id: str,
) -> ActionIntent:
    """Build the exact canonical high-impact action that must be approved."""

    service = next(
        (
            item
            for item in plan.services
            if item.service_id == service_id
        ),
        None,
    )
    if service is None:
        raise DeploymentPromotionError(
            "production approval action references an unknown service"
        )
    target = json.dumps(
        {
            "artifact_digest": service.release.artifact_digest,
            "production_environment_id": (
                service.production.intent.environment.environment_id
            ),
            "project_id": plan.project_id,
            "release_sha": service.release.source_sha,
            "service_id": service.service_id,
            "staging_environment_id": (
                service.staging.intent.environment.environment_id
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ActionIntent(
        action_id=f"production-promotion:{plan.plan_id}:{service.service_id}",
        tool_id="deployment.production.promote",
        risk=ToolRisk.HIGH_IMPACT,
        target=target,
        approval_required=True,
    )


def build_promotion_wave_plan(
    plan: DeploymentPromotionPlan,
) -> DeploymentWavePlan:
    services: list[ServiceRolloutSpec] = []
    for service in sorted(
        plan.services,
        key=lambda item: (item.batch, item.service_id),
    ):
        staging_id = _stage_service_id(
            service.service_id,
            EnvironmentTier.STAGING,
        )
        production_id = _stage_service_id(
            service.service_id,
            EnvironmentTier.PRODUCTION,
        )
        staging_dependencies = tuple(
            _stage_service_id(
                parent,
                EnvironmentTier.PRODUCTION,
            )
            for parent in sorted(service.depends_on)
        )
        services.extend(
            (
                ServiceRolloutSpec(
                    staging_id,
                    service.batch * 2,
                    service.staging,
                    staging_dependencies,
                ),
                ServiceRolloutSpec(
                    production_id,
                    service.batch * 2 + 1,
                    service.production,
                    (staging_id,),
                ),
            )
        )
    return DeploymentWavePlan(
        _wave_plan_id(plan.plan_id),
        plan.project_id,
        tuple(services),
    )


def _stage_service_id(
    service_id: str,
    tier: EnvironmentTier,
) -> str:
    return f"{service_id}@{tier.value}"


def _wave_plan_id(plan_id: str) -> str:
    return f"promotion:{plan_id}"


_TERMINAL_STATES = {
    OperationState.SUCCEEDED,
    OperationState.REJECTED,
    OperationState.ROLLED_BACK,
}
