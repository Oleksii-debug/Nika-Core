from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from math import isfinite
from statistics import fmean
from time import monotonic_ns
from typing import Callable

from nika_core.experiments.contracts import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentSnapshot,
    ExperimentStatus,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import ExperimentRepository
from nika_core.model_gateway.contracts import ModelRequest, ModelResponse
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_lab.contracts import (
    BenchmarkReport,
    CandidateBenchmarkSummary,
    EvaluationCase,
    EvaluationSuite,
    ModelBenchmarkPolicy,
    ModelCandidate,
    QualityScorer,
)
from nika_core.resources.contracts import ResourceObserverPort

_QUALITY = "quality"
_LATENCY_MS = "latency_ms"
_HOST_CPU_PERCENT = "host_cpu_percent"
_HOST_MEMORY_PERCENT = "host_memory_percent"


class ModelEngineeringLab:
    """Thin model benchmark orchestration over ModelGateway and the M8 experiment engine."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        repository: ExperimentRepository,
        resource_observer: ResourceObserverPort | None = None,
        clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._gateway = gateway
        self._repository = repository
        self._engine = ExperimentEngine(repository)
        self._resource_observer = resource_observer
        self._clock_ns = clock_ns

    async def compare(
        self,
        *,
        experiment_id: str,
        champion: ModelCandidate,
        challengers: tuple[ModelCandidate, ...],
        suite: EvaluationSuite,
        policy: ModelBenchmarkPolicy,
        scorer: QualityScorer,
    ) -> BenchmarkReport:
        definition = self._definition(
            experiment_id=experiment_id,
            champion=champion,
            challengers=challengers,
            suite=suite,
            policy=policy,
        )
        snapshot = self._load_or_create(definition)
        if snapshot.status is ExperimentStatus.DRAFT:
            snapshot = self._engine.start(experiment_id)
        elif snapshot.status is not ExperimentStatus.RUNNING:
            return self._report(snapshot=snapshot, suite=suite)

        metric_names = self._metric_names(policy)
        self._validate_resume_groups(snapshot=snapshot, metric_names=metric_names)
        candidates = (champion, *challengers)
        for candidate in candidates:
            for case in suite.cases:
                snapshot = self._repository.get(experiment_id)
                if self._group_is_complete(
                    snapshot=snapshot,
                    candidate_id=candidate.candidate_id,
                    replay_id=case.case_id,
                    metric_names=metric_names,
                ):
                    continue
                observations = await self._evaluate_case(
                    experiment_id=experiment_id,
                    candidate=candidate,
                    case=case,
                    suite=suite,
                    policy=policy,
                    scorer=scorer,
                )
                self._append_observation_batch(experiment_id, observations)

        completed = self._engine.complete(experiment_id)
        return self._report(snapshot=completed, suite=suite)

    def _load_or_create(self, definition: ExperimentDefinition) -> ExperimentSnapshot:
        try:
            snapshot = self._repository.get(definition.experiment_id)
        except KeyError:
            return self._engine.create(definition)
        if snapshot.definition != definition:
            raise ValueError("existing model-lab experiment definition does not match resume request")
        return snapshot

    def _definition(
        self,
        *,
        experiment_id: str,
        champion: ModelCandidate,
        challengers: tuple[ModelCandidate, ...],
        suite: EvaluationSuite,
        policy: ModelBenchmarkPolicy,
    ) -> ExperimentDefinition:
        if not experiment_id.strip():
            raise ValueError("experiment_id must not be empty")
        if experiment_id != experiment_id.strip():
            raise ValueError("experiment_id must not contain surrounding whitespace")
        if not challengers:
            raise ValueError("at least one challenger is required")
        if len(suite.cases) < policy.minimum_cases:
            raise ValueError("evaluation suite is smaller than minimum_cases")
        if self._resource_metrics_requested(policy) and self._resource_observer is None:
            raise ValueError("host resource guardrails require a resource observer")

        return ExperimentDefinition(
            experiment_id=experiment_id,
            champion=self._strategy_ref(champion),
            challengers=tuple(self._strategy_ref(candidate) for candidate in challengers),
            replays=tuple(
                ReplayCase(
                    replay_id=case.case_id,
                    dataset_ref=suite.dataset_ref,
                    dataset_version=suite.evidence_version,
                )
                for case in suite.cases
            ),
            policy=PromotionPolicy(
                primary_metric=_QUALITY,
                primary_higher_is_better=True,
                minimum_improvement=policy.minimum_quality_improvement,
                minimum_replays=policy.minimum_cases,
                guardrails=self._guardrails(policy),
            ),
        )

    @staticmethod
    def _strategy_ref(candidate: ModelCandidate) -> StrategyRef:
        return StrategyRef(
            candidate_id=candidate.candidate_id,
            version=candidate.version,
            artifact_kind=ArtifactKind.CONFIG,
            artifact_ref=candidate.artifact_ref,
            permission_fingerprint=candidate.permission_fingerprint,
        )

    @staticmethod
    def _guardrails(policy: ModelBenchmarkPolicy) -> tuple[MetricRule, ...]:
        rules = [
            MetricRule(
                metric=_LATENCY_MS,
                higher_is_better=False,
                max_regression=policy.max_latency_regression_ms,
            )
        ]
        if policy.max_host_cpu_regression_percent is not None:
            rules.append(
                MetricRule(
                    metric=_HOST_CPU_PERCENT,
                    higher_is_better=False,
                    max_regression=policy.max_host_cpu_regression_percent,
                )
            )
        if policy.max_host_memory_regression_percent is not None:
            rules.append(
                MetricRule(
                    metric=_HOST_MEMORY_PERCENT,
                    higher_is_better=False,
                    max_regression=policy.max_host_memory_regression_percent,
                )
            )
        return tuple(rules)

    @classmethod
    def _metric_names(cls, policy: ModelBenchmarkPolicy) -> tuple[str, ...]:
        return (_QUALITY, *(rule.metric for rule in cls._guardrails(policy)))

    @staticmethod
    def _resource_metrics_requested(policy: ModelBenchmarkPolicy) -> bool:
        return (
            policy.max_host_cpu_regression_percent is not None
            or policy.max_host_memory_regression_percent is not None
        )

    async def _evaluate_case(
        self,
        *,
        experiment_id: str,
        candidate: ModelCandidate,
        case: EvaluationCase,
        suite: EvaluationSuite,
        policy: ModelBenchmarkPolicy,
        scorer: QualityScorer,
    ) -> tuple[MetricObservation, ...]:
        before = None
        if self._resource_metrics_requested(policy):
            assert self._resource_observer is not None
            before = self._resource_observer.snapshot()
        started_ns = self._clock_ns()
        request = ModelRequest(
            request_id=f"model-lab:{experiment_id}:{candidate.candidate_id}:{case.case_id}",
            messages=case.messages,
            model=candidate.model,
            provider_id=candidate.provider_id,
            privacy=suite.privacy,
            timeout_seconds=policy.request_timeout_seconds,
            temperature=0.0,
            metadata={
                "model_lab_experiment_id": experiment_id,
                "model_lab_candidate_id": candidate.candidate_id,
                "model_lab_case_id": case.case_id,
                "model_lab_dataset_sha256": suite.content_sha256,
            },
        )
        response = await self._gateway.complete(request)
        finished_ns = self._clock_ns()
        if finished_ns < started_ns:
            raise RuntimeError("monotonic model-lab clock moved backwards")
        self._validate_response_identity(request=request, candidate=candidate, response=response)
        quality = float(scorer.score(case, response))
        if not isfinite(quality):
            raise ValueError("quality scorer must return a finite value")
        latency_ms = (finished_ns - started_ns) / 1_000_000.0

        observations = [
            MetricObservation(
                candidate_id=candidate.candidate_id,
                replay_id=case.case_id,
                metric=_QUALITY,
                value=quality,
            ),
            MetricObservation(
                candidate_id=candidate.candidate_id,
                replay_id=case.case_id,
                metric=_LATENCY_MS,
                value=latency_ms,
            ),
        ]
        if before is not None:
            assert self._resource_observer is not None
            after = self._resource_observer.snapshot()
            if policy.max_host_cpu_regression_percent is not None:
                observations.append(
                    MetricObservation(
                        candidate_id=candidate.candidate_id,
                        replay_id=case.case_id,
                        metric=_HOST_CPU_PERCENT,
                        value=max(float(before.cpu_percent), float(after.cpu_percent)),
                    )
                )
            if policy.max_host_memory_regression_percent is not None:
                observations.append(
                    MetricObservation(
                        candidate_id=candidate.candidate_id,
                        replay_id=case.case_id,
                        metric=_HOST_MEMORY_PERCENT,
                        value=max(float(before.memory_percent), float(after.memory_percent)),
                    )
                )
        return tuple(observations)

    @staticmethod
    def _validate_response_identity(
        *, request: ModelRequest, candidate: ModelCandidate, response: ModelResponse
    ) -> None:
        if response.request_id != request.request_id:
            raise RuntimeError("model response request identity does not match benchmark request")
        if response.provider_id != candidate.provider_id:
            raise RuntimeError("model response provider identity does not match benchmark candidate")
        if response.model != candidate.model:
            raise RuntimeError("model response model identity does not match benchmark candidate")

    def _append_observation_batch(
        self,
        experiment_id: str,
        observations: tuple[MetricObservation, ...],
    ) -> None:
        batch_map = {self._observation_key(item): item for item in observations}
        if len(batch_map) != len(observations):
            raise ValueError("model-lab observation batch contains duplicate metric keys")
        for _attempt in range(3):
            snapshot = self._repository.get(experiment_id)
            if snapshot.status is not ExperimentStatus.RUNNING:
                raise ValueError("model-lab evidence requires a running experiment")
            existing = {self._observation_key(item): item for item in snapshot.observations}
            present = [key for key in batch_map if key in existing]
            if present:
                if len(present) != len(batch_map):
                    raise RuntimeError("partial model-lab observation group detected")
                if any(float(existing[key].value) != float(batch_map[key].value) for key in batch_map):
                    raise RuntimeError("conflicting model-lab observation group detected")
                return
            proposed = replace(snapshot, observations=(*snapshot.observations, *observations))
            try:
                self._repository.save(proposed)
            except ValueError as exc:
                if str(exc) != "experiment evidence is append-only":
                    raise
                continue
            return
        raise RuntimeError("concurrent model-lab evidence writers did not converge")

    @classmethod
    def _validate_resume_groups(
        cls, *, snapshot: ExperimentSnapshot, metric_names: tuple[str, ...]
    ) -> None:
        for candidate in (
            snapshot.definition.champion,
            *snapshot.definition.challengers,
        ):
            for replay in snapshot.definition.replays:
                group = [
                    item
                    for item in snapshot.observations
                    if item.candidate_id == candidate.candidate_id
                    and item.replay_id == replay.replay_id
                ]
                keys = {item.metric for item in group}
                if len(group) != len(keys):
                    raise RuntimeError(
                        "duplicate model-lab observation metric detected for "
                        f"{candidate.candidate_id}/{replay.replay_id}"
                    )
                expected = set(metric_names)
                if keys and keys != expected:
                    raise RuntimeError(
                        "partial model-lab observation group detected for "
                        f"{candidate.candidate_id}/{replay.replay_id}"
                    )

    @staticmethod
    def _group_is_complete(
        *,
        snapshot: ExperimentSnapshot,
        candidate_id: str,
        replay_id: str,
        metric_names: tuple[str, ...],
    ) -> bool:
        keys = {
            item.metric
            for item in snapshot.observations
            if item.candidate_id == candidate_id and item.replay_id == replay_id
        }
        return keys == set(metric_names)

    @staticmethod
    def _observation_key(item: MetricObservation) -> tuple[str, str, str]:
        return (item.candidate_id, item.replay_id, item.metric)

    @staticmethod
    def _report(*, snapshot: ExperimentSnapshot, suite: EvaluationSuite) -> BenchmarkReport:
        by_candidate: defaultdict[str, defaultdict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for observation in snapshot.observations:
            by_candidate[observation.candidate_id][observation.metric].append(
                float(observation.value)
            )
        summaries = []
        for candidate in (
            snapshot.definition.champion,
            *snapshot.definition.challengers,
        ):
            metrics = by_candidate[candidate.candidate_id]
            if not metrics:
                continue
            summaries.append(
                CandidateBenchmarkSummary(
                    candidate_id=candidate.candidate_id,
                    quality_mean=fmean(metrics[_QUALITY]),
                    latency_mean_ms=fmean(metrics[_LATENCY_MS]),
                    host_cpu_mean_percent=(
                        fmean(metrics[_HOST_CPU_PERCENT]) if metrics[_HOST_CPU_PERCENT] else None
                    ),
                    host_memory_mean_percent=(
                        fmean(metrics[_HOST_MEMORY_PERCENT])
                        if metrics[_HOST_MEMORY_PERCENT]
                        else None
                    ),
                )
            )
        return BenchmarkReport(
            experiment_id=snapshot.definition.experiment_id,
            status=snapshot.status,
            selected_candidate_id=snapshot.selected_candidate_id,
            dataset_sha256=suite.content_sha256,
            summaries=tuple(summaries),
        )
