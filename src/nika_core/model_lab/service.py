from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from time import perf_counter
from typing import Callable, Mapping

from nika_core.experiments.contracts import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
    ReplayCase,
    StrategyRef,
)
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import ExperimentRepository
from nika_core.model_gateway.contracts import ModelRequest, ModelResponse
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_lab.contracts import (
    BenchmarkCandidate,
    BenchmarkCase,
    BenchmarkEvaluator,
    BenchmarkPlan,
    canonical_digest,
)


class BenchmarkDefinitionMismatchError(ValueError):
    pass


class BenchmarkEvidenceIntegrityError(RuntimeError):
    pass


class BenchmarkResponseIdentityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BenchmarkRunResult:
    snapshot: ExperimentSnapshot
    executed_cases: int
    reused_cases: int


class ModelEngineeringLab:
    """Thin evaluation orchestrator over ModelGateway and ExperimentEngine."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        experiment_engine: ExperimentEngine,
        experiment_repository: ExperimentRepository,
        evaluator: BenchmarkEvaluator,
        monotonic: Callable[[], float] = perf_counter,
    ) -> None:
        self._gateway = gateway
        self._engine = experiment_engine
        self._repository = experiment_repository
        self._evaluator = evaluator
        self._monotonic = monotonic
        if not evaluator.evaluator_id.strip() or not evaluator.version.strip():
            raise ValueError("benchmark evaluator identity must be complete")

    def compile_definition(self, plan: BenchmarkPlan) -> ExperimentDefinition:
        return ExperimentDefinition(
            experiment_id=self._experiment_id(plan.benchmark_id),
            champion=self._strategy_ref(plan.champion),
            challengers=tuple(self._strategy_ref(item) for item in plan.challengers),
            replays=tuple(self._replay_case(plan, item) for item in plan.cases),
            policy=plan.policy,
        )

    async def run(self, plan: BenchmarkPlan) -> BenchmarkRunResult:
        definition = self.compile_definition(plan)
        snapshot = self._load_or_create(definition)
        case_count = (1 + len(plan.challengers)) * len(plan.cases)

        if snapshot.status in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.PROMOTED,
            ExperimentStatus.ROLLED_BACK,
        }:
            return BenchmarkRunResult(
                snapshot=snapshot,
                executed_cases=0,
                reused_cases=case_count,
            )
        if snapshot.status is ExperimentStatus.DRAFT:
            snapshot = self._engine.start(definition.experiment_id)
        if snapshot.status is not ExperimentStatus.RUNNING:
            raise BenchmarkEvidenceIntegrityError(
                f"benchmark is not runnable from status: {snapshot.status.value}"
            )

        required_metrics = self._required_metrics(plan)
        executed_cases = 0
        reused_cases = 0
        candidates = (plan.champion, *plan.challengers)

        for candidate in candidates:
            for case in plan.cases:
                snapshot = self._repository.get(definition.experiment_id)
                existing = self._existing_metrics(
                    snapshot,
                    candidate.candidate_id,
                    case.case_id,
                )
                present = existing.intersection(required_metrics)
                if present == required_metrics:
                    reused_cases += 1
                    continue
                if present:
                    missing = sorted(required_metrics - present)
                    raise BenchmarkEvidenceIntegrityError(
                        "partial candidate/case evidence cannot be mixed with a new "
                        "model response: "
                        f"{candidate.candidate_id}/{case.case_id}; missing={missing}"
                    )
                observations = await self._evaluate_case(
                    plan=plan,
                    candidate=candidate,
                    case=case,
                    required_metrics=required_metrics,
                )
                for observation in observations:
                    self._engine.record(definition.experiment_id, observation)
                executed_cases += 1

        completed = self._engine.complete(definition.experiment_id)
        return BenchmarkRunResult(
            snapshot=completed,
            executed_cases=executed_cases,
            reused_cases=reused_cases,
        )

    def rollback(self, benchmark_id: str) -> ExperimentSnapshot:
        if not benchmark_id.strip():
            raise ValueError("benchmark_id must not be empty")
        return self._engine.rollback(self._experiment_id(benchmark_id))

    def _load_or_create(self, definition: ExperimentDefinition) -> ExperimentSnapshot:
        try:
            snapshot = self._repository.get(definition.experiment_id)
        except KeyError:
            return self._engine.create(definition)
        if snapshot.definition != definition:
            raise BenchmarkDefinitionMismatchError(
                "benchmark_id already exists with different immutable candidate, suite, "
                "evaluator, request, or policy identity"
            )
        return snapshot

    async def _evaluate_case(
        self,
        *,
        plan: BenchmarkPlan,
        candidate: BenchmarkCandidate,
        case: BenchmarkCase,
        required_metrics: set[str],
    ) -> tuple[MetricObservation, ...]:
        request = ModelRequest(
            request_id=self._request_id(plan, candidate, case),
            messages=case.messages,
            model=candidate.model,
            provider_id=candidate.provider_id,
            privacy=case.privacy,
            timeout_seconds=case.timeout_seconds,
            temperature=plan.temperature,
            metadata={
                "benchmark_id": plan.benchmark_id,
                "candidate_id": candidate.candidate_id,
                "case_id": case.case_id,
                "suite_fingerprint": self._case_fingerprint(plan, case),
            },
        )
        started = self._monotonic()
        response = await self._gateway.complete(request)
        elapsed_ms = (self._monotonic() - started) * 1000.0
        if not isfinite(elapsed_ms) or elapsed_ms < 0:
            raise BenchmarkEvidenceIntegrityError(
                "benchmark clock returned invalid elapsed time"
            )
        self._validate_response_identity(candidate, response)

        metric_values = dict(self._system_metrics(response, elapsed_ms))
        evaluator_values = dict(self._evaluator.evaluate(case, response))
        overlap = set(metric_values).intersection(evaluator_values)
        if overlap:
            raise BenchmarkEvidenceIntegrityError(
                "evaluator attempted to overwrite reserved system metric(s): "
                f"{sorted(overlap)}"
            )
        metric_values.update(evaluator_values)

        missing = sorted(required_metrics - set(metric_values))
        if missing:
            raise BenchmarkEvidenceIntegrityError(
                "benchmark evaluator/provider did not produce required metric(s): "
                f"{missing}"
            )
        observations: list[MetricObservation] = []
        for metric in self._ordered_metrics(plan):
            value = float(metric_values[metric])
            if not isfinite(value):
                raise BenchmarkEvidenceIntegrityError(
                    f"benchmark metric must be finite: {metric}"
                )
            observations.append(
                MetricObservation(
                    candidate_id=candidate.candidate_id,
                    replay_id=case.case_id,
                    metric=metric,
                    value=value,
                )
            )
        return tuple(observations)

    @staticmethod
    def _validate_response_identity(
        candidate: BenchmarkCandidate,
        response: ModelResponse,
    ) -> None:
        if response.provider_id != candidate.provider_id:
            raise BenchmarkResponseIdentityError(
                "model provider response identity differs from the benchmark candidate"
            )
        if response.model != candidate.expected_model_id:
            raise BenchmarkResponseIdentityError(
                "model response identity differs from the benchmark candidate"
            )

    @staticmethod
    def _system_metrics(
        response: ModelResponse,
        elapsed_ms: float,
    ) -> Mapping[str, float]:
        values: dict[str, float] = {"gateway_latency_ms": elapsed_ms}
        if response.latency_ms is not None:
            values["provider_latency_ms"] = float(response.latency_ms)
        for name, value in (
            ("input_tokens", response.usage.input_tokens),
            ("output_tokens", response.usage.output_tokens),
            ("total_tokens", response.usage.total_tokens),
        ):
            if value is not None:
                values[name] = float(value)
        return values

    def _strategy_ref(self, candidate: BenchmarkCandidate) -> StrategyRef:
        fingerprint = canonical_digest(
            {
                "provider_id": candidate.provider_id,
                "model": candidate.model,
                "expected_model_id": candidate.expected_model_id,
                "candidate_version": candidate.version,
                "artifact_ref": candidate.artifact_ref,
            }
        )
        return StrategyRef(
            candidate_id=candidate.candidate_id,
            version=candidate.version,
            artifact_kind=ArtifactKind.CONFIG,
            artifact_ref=(
                f"{candidate.artifact_ref}#model-lab-sha256={fingerprint}"
            ),
            permission_fingerprint=candidate.permission_fingerprint,
        )

    def _replay_case(self, plan: BenchmarkPlan, case: BenchmarkCase) -> ReplayCase:
        return ReplayCase(
            replay_id=case.case_id,
            dataset_ref=case.dataset_ref,
            dataset_version=(
                f"{case.dataset_version}@model-lab-sha256="
                f"{self._case_fingerprint(plan, case)}"
            ),
        )

    def _case_fingerprint(self, plan: BenchmarkPlan, case: BenchmarkCase) -> str:
        return canonical_digest(
            {
                "messages": [
                    {"role": item.role, "content": item.content}
                    for item in case.messages
                ],
                "reference": list(case.reference),
                "privacy": case.privacy.value,
                "timeout_seconds": case.timeout_seconds,
                "temperature": plan.temperature,
                "plan_metadata": list(plan.metadata),
                "evaluator_id": self._evaluator.evaluator_id,
                "evaluator_version": self._evaluator.version,
            }
        )

    def _request_id(
        self,
        plan: BenchmarkPlan,
        candidate: BenchmarkCandidate,
        case: BenchmarkCase,
    ) -> str:
        digest = canonical_digest(
            {
                "experiment": self._experiment_id(plan.benchmark_id),
                "candidate": candidate.candidate_id,
                "case": case.case_id,
                "case_fingerprint": self._case_fingerprint(plan, case),
            }
        )
        return f"model-lab:{digest}"

    @staticmethod
    def _experiment_id(benchmark_id: str) -> str:
        return f"model-lab:{benchmark_id}"

    @staticmethod
    def _existing_metrics(
        snapshot: ExperimentSnapshot,
        candidate_id: str,
        replay_id: str,
    ) -> set[str]:
        return {
            item.metric
            for item in snapshot.observations
            if item.candidate_id == candidate_id and item.replay_id == replay_id
        }

    @staticmethod
    def _required_metrics(plan: BenchmarkPlan) -> set[str]:
        return {
            plan.policy.primary_metric,
            *(item.metric for item in plan.policy.guardrails),
        }

    @staticmethod
    def _ordered_metrics(plan: BenchmarkPlan) -> tuple[str, ...]:
        return (
            plan.policy.primary_metric,
            *(item.metric for item in plan.policy.guardrails),
        )
