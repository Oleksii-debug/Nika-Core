from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Sequence
from math import ceil, isfinite
from statistics import fmean
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from nika_core.model_engineering.contracts import (
    AcceleratorObserverPort,
    AcceleratorSnapshot,
    BenchmarkExecutionConfig,
    BenchmarkRunEvidence,
    BenchmarkSuiteReport,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationCase,
    EvaluationSet,
    ModelCandidate,
    benchmark_configuration_sha256,
)
from nika_core.model_gateway.contracts import (
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot


class ModelCompletionPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ModelScoringPort(Protocol):
    def score(self, case: EvaluationCase, response: ModelResponse) -> float: ...


class ModelBenchmarkError(RuntimeError):
    pass


class ModelBenchmarkIdentityError(ModelBenchmarkError):
    pass


class ExactMatchScorer:
    """Deterministic Unicode-normalized exact-match scorer."""

    @staticmethod
    def _normalize(value: str) -> str:
        return unicodedata.normalize("NFC", value).strip()

    def score(self, case: EvaluationCase, response: ModelResponse) -> float:
        return float(
            self._normalize(response.text) == self._normalize(case.expected_text)
        )


_DEFAULT_SCORER_ID = "exact-match-nfc-v1"


def _default_run_id() -> str:
    return f"run-{uuid4().hex}"


class ModelBenchmarkRunner:
    def __init__(
        self,
        gateway: ModelCompletionPort,
        *,
        scorer: ModelScoringPort | None = None,
        scorer_id: str | None = None,
        resource_observer: ResourceObserverPort | None = None,
        accelerator_observer: AcceleratorObserverPort | None = None,
        clock: Callable[[], float] = perf_counter,
        run_id_factory: Callable[[], str] = _default_run_id,
    ) -> None:
        self._gateway = gateway
        if scorer is None:
            if scorer_id is not None and scorer_id != _DEFAULT_SCORER_ID:
                raise ValueError(
                    "default ExactMatchScorer requires the canonical scorer_id"
                )
            self._scorer = ExactMatchScorer()
            self._scorer_id = _DEFAULT_SCORER_ID
        else:
            if type(scorer_id) is not str:
                raise TypeError("custom scorer requires a canonical scorer_id")
            if not scorer_id or scorer_id != scorer_id.strip():
                raise ValueError(
                    "custom scorer_id must be non-empty without surrounding whitespace"
                )
            self._scorer = scorer
            self._scorer_id = scorer_id
        self._resource_observer = resource_observer
        self._accelerator_observer = accelerator_observer
        self._clock = clock
        self._run_id_factory = run_id_factory

    async def benchmark(
        self,
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        *,
        timeout_seconds: float = 60.0,
        temperature: float | None = 0.0,
    ) -> CandidateBenchmarkReport:
        if type(candidate) is not ModelCandidate:
            raise TypeError("candidate must be an exact ModelCandidate")
        if type(evaluation_set) is not EvaluationSet:
            raise TypeError("evaluation_set must be an exact EvaluationSet")
        execution_config = BenchmarkExecutionConfig(
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            scorer_id=self._scorer_id,
        )
        run_id = self._new_run_id()
        configuration_sha256 = benchmark_configuration_sha256(
            candidate_evidence_sha256=candidate.evidence_sha256,
            evaluation_set_id=evaluation_set.evaluation_set_id,
            evaluation_set_version=evaluation_set.version,
            evaluation_set_sha256=evaluation_set.content_sha256,
            execution_config_sha256=execution_config.evidence_sha256,
        )

        results: list[CaseBenchmarkResult] = []
        for case in evaluation_set.cases:
            results.append(
                await self._run_case(
                    candidate,
                    evaluation_set,
                    case,
                    execution_config=execution_config,
                    run_id=run_id,
                    configuration_sha256=configuration_sha256,
                )
            )
        return self._build_report(
            candidate,
            evaluation_set,
            execution_config,
            tuple(results),
            run_id=run_id,
            configuration_sha256=configuration_sha256,
        )

    async def benchmark_suite(
        self,
        candidates: Sequence[ModelCandidate],
        evaluation_set: EvaluationSet,
        *,
        timeout_seconds: float = 60.0,
        temperature: float | None = 0.0,
    ) -> BenchmarkSuiteReport:
        if type(evaluation_set) is not EvaluationSet:
            raise TypeError("evaluation_set must be an exact EvaluationSet")
        if type(candidates) is not tuple:
            raise TypeError("benchmark suite candidates must be a canonical tuple")
        if not candidates:
            raise ValueError("benchmark suite requires at least one candidate")
        if any(type(candidate) is not ModelCandidate for candidate in candidates):
            raise TypeError("benchmark suite candidates must use exact ModelCandidate values")
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark suite candidate IDs must be unique")

        execution_config = BenchmarkExecutionConfig(
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            scorer_id=self._scorer_id,
        )
        reports = []
        for candidate in candidates:
            reports.append(
                await self.benchmark(
                    candidate,
                    evaluation_set,
                    timeout_seconds=timeout_seconds,
                    temperature=temperature,
                )
            )
        return BenchmarkSuiteReport(
            evaluation_set_id=evaluation_set.evaluation_set_id,
            evaluation_set_version=evaluation_set.version,
            evaluation_set_sha256=evaluation_set.content_sha256,
            execution_config_sha256=execution_config.evidence_sha256,
            reports=tuple(reports),
        )

    async def _run_case(
        self,
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        case: EvaluationCase,
        *,
        execution_config: BenchmarkExecutionConfig,
        run_id: str,
        configuration_sha256: str,
    ) -> CaseBenchmarkResult:
        resource_before = self._resource_snapshot()
        accelerator_before = self._accelerator_snapshot()
        request = ModelRequest(
            request_id=self._request_id(
                candidate,
                evaluation_set,
                case,
                run_id,
                configuration_sha256,
            ),
            messages=case.messages,
            model=candidate.request_model,
            provider_id=candidate.provider_id,
            provider_kind=candidate.provider_kind,
            privacy=evaluation_set.privacy,
            timeout_seconds=execution_config.timeout_seconds,
            temperature=execution_config.temperature,
            metadata={
                "evaluation_set_id": evaluation_set.evaluation_set_id,
                "evaluation_set_version": evaluation_set.version,
                "evaluation_set_sha256": evaluation_set.content_sha256,
                "evaluation_case_id": case.case_id,
                "model_candidate_id": candidate.candidate_id,
                "benchmark_execution_config_sha256": execution_config.evidence_sha256,
                "benchmark_run_id": run_id,
                "benchmark_configuration_sha256": configuration_sha256,
            },
        )
        started = self._clock()
        if type(started) not in (int, float) or not isfinite(float(started)):
            raise ModelBenchmarkError("benchmark clock returned a non-finite numeric carrier")
        try:
            response = await self._gateway.complete(request)
        except ModelGatewayError as error:
            latency_ms = self._elapsed_ms(started)
            resource_after = self._resource_snapshot()
            accelerator_after = self._accelerator_snapshot()
            return CaseBenchmarkResult(
                candidate_id=candidate.candidate_id,
                case_id=case.case_id,
                evaluation_weight=float(case.weight),
                score=0.0,
                passed=False,
                completion_succeeded=False,
                latency_ms=latency_ms,
                response_sha256=None,
                error_code=error.code,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                resource_before=resource_before,
                resource_after=resource_after,
                accelerator_before=accelerator_before,
                accelerator_after=accelerator_after,
            )

        self._validate_response_identity(candidate, request, response)
        input_tokens, output_tokens, total_tokens = self._usage(response)
        latency_ms = self._elapsed_ms(started)
        resource_after = self._resource_snapshot()
        accelerator_after = self._accelerator_snapshot()
        raw_score = self._scorer.score(case, response)
        if type(raw_score) not in (int, float):
            raise ModelBenchmarkError("scorer returned a non-canonical numeric score")
        score = float(raw_score)
        if not isfinite(score) or not 0 <= score <= 1:
            raise ModelBenchmarkError("scorer returned a non-finite or out-of-range score")
        return CaseBenchmarkResult(
            candidate_id=candidate.candidate_id,
            case_id=case.case_id,
            evaluation_weight=float(case.weight),
            score=score,
            passed=score >= float(case.pass_score),
            completion_succeeded=True,
            latency_ms=latency_ms,
            response_sha256=hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
            error_code=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            resource_before=resource_before,
            resource_after=resource_after,
            accelerator_before=accelerator_before,
            accelerator_after=accelerator_after,
        )

    def _elapsed_ms(self, started: float) -> float:
        finished = self._clock()
        if type(finished) not in (int, float) or not isfinite(float(finished)):
            raise ModelBenchmarkError("benchmark clock returned a non-finite numeric carrier")
        elapsed = (finished - started) * 1000.0
        if not isfinite(elapsed) or elapsed < 0:
            raise ModelBenchmarkError("benchmark clock moved backwards or became non-finite")
        return elapsed

    @staticmethod
    def _request_id(
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        case: EvaluationCase,
        run_id: str,
        configuration_sha256: str,
    ) -> str:
        del candidate, evaluation_set
        raw = (
            f"nika-model-benchmark-v3\0{run_id}\0"
            f"{configuration_sha256}\0{case.case_id}"
        ).encode()
        return f"model-bench-{hashlib.sha256(raw).hexdigest()[:32]}"

    def _new_run_id(self) -> str:
        run_id = self._run_id_factory()
        BenchmarkRunEvidence(
            run_id=run_id,
            configuration_sha256="0" * 64,
        )
        return run_id

    @staticmethod
    def _validate_response_identity(
        candidate: ModelCandidate,
        request: ModelRequest,
        response: ModelResponse,
    ) -> None:
        if type(response) is not ModelResponse:
            raise ModelBenchmarkError("gateway returned an invalid response carrier")
        for value, name in (
            (response.request_id, "request_id"),
            (response.text, "text"),
            (response.provider_id, "provider_id"),
            (response.model, "model"),
        ):
            if type(value) is not str:
                raise ModelBenchmarkError(f"response {name} must be canonical text")
        if not any(response.provider_kind is member for member in ProviderKind):
            raise ModelBenchmarkError("response provider_kind must be canonical")
        if type(response.usage) is not ModelUsage:
            raise ModelBenchmarkError("response usage must be an exact ModelUsage")
        try:
            response.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ModelBenchmarkError(
                "successful benchmark response text must be valid UTF-8"
            ) from exc
        if response.request_id != request.request_id:
            raise ModelBenchmarkIdentityError("response request identity mismatch")
        if response.provider_id != candidate.provider_id:
            raise ModelBenchmarkIdentityError("response provider identity mismatch")
        if response.provider_kind != candidate.provider_kind:
            raise ModelBenchmarkIdentityError("response provider kind mismatch")
        if response.model != candidate.expected_response_model:
            raise ModelBenchmarkIdentityError("response model identity mismatch")
        if not response.text:
            raise ModelBenchmarkError("successful benchmark response text must not be empty")

    @staticmethod
    def _usage(response: ModelResponse) -> tuple[int | None, int | None, int | None]:
        values = (
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.usage.total_tokens,
        )
        for value in values:
            if value is not None and (type(value) is not int or value < 0):
                raise ModelBenchmarkError("model usage must use non-negative integer counts")
        input_tokens, output_tokens, total_tokens = values
        if (
            total_tokens is not None
            and input_tokens is not None
            and output_tokens is not None
            and total_tokens < input_tokens + output_tokens
        ):
            raise ModelBenchmarkError("total_tokens is smaller than known token components")
        return input_tokens, output_tokens, total_tokens

    def _resource_snapshot(self) -> ResourceSnapshot | None:
        if self._resource_observer is None:
            return None
        snapshot = self._resource_observer.snapshot()
        if type(snapshot) is not ResourceSnapshot:
            raise ModelBenchmarkError("resource observer returned an invalid snapshot type")
        if type(snapshot.cpu_percent) not in (int, float):
            raise ModelBenchmarkError("resource observer returned invalid CPU percent")
        if type(snapshot.memory_percent) not in (int, float):
            raise ModelBenchmarkError("resource observer returned invalid memory percent")
        cpu = float(snapshot.cpu_percent)
        memory = float(snapshot.memory_percent)
        available = snapshot.available_memory_bytes
        if not isfinite(cpu) or not 0 <= cpu <= 100:
            raise ModelBenchmarkError("resource observer returned invalid CPU percent")
        if not isfinite(memory) or not 0 <= memory <= 100:
            raise ModelBenchmarkError("resource observer returned invalid memory percent")
        if type(available) is not int or available < 0:
            raise ModelBenchmarkError("resource observer returned invalid available memory")
        process_rss = snapshot.process_rss_bytes
        if process_rss is not None and (
            type(process_rss) is not int or process_rss < 0
        ):
            raise ModelBenchmarkError("resource observer returned invalid process RSS")
        return snapshot

    def _accelerator_snapshot(self) -> AcceleratorSnapshot | None:
        if self._accelerator_observer is None:
            return None
        try:
            snapshot = self._accelerator_observer.snapshot()
        except ValueError as exc:
            raise ModelBenchmarkError("accelerator observer returned invalid telemetry") from exc
        if type(snapshot) is not AcceleratorSnapshot:
            raise ModelBenchmarkError("accelerator observer returned an invalid snapshot type")
        return snapshot

    @staticmethod
    def _build_report(
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        execution_config: BenchmarkExecutionConfig,
        results: tuple[CaseBenchmarkResult, ...],
        *,
        run_id: str,
        configuration_sha256: str,
    ) -> CandidateBenchmarkReport:
        total_weight = sum(float(result.evaluation_weight) for result in results)
        quality = sum(
            float(result.score) * float(result.evaluation_weight)
            for result in results
        ) / total_weight
        pass_rate = sum(result.passed for result in results) / len(results)
        completion_rate = (
            sum(result.completion_succeeded for result in results) / len(results)
        )
        latencies = [
            result.latency_ms for result in results if result.completion_succeeded
        ]
        resource_snapshots = [
            snapshot
            for result in results
            for snapshot in (result.resource_before, result.resource_after)
            if snapshot is not None
        ]
        accelerator_snapshots = [
            snapshot
            for result in results
            for snapshot in (result.accelerator_before, result.accelerator_after)
            if snapshot is not None
        ]
        utilization = [
            float(snapshot.utilization_percent)
            for snapshot in accelerator_snapshots
            if snapshot.utilization_percent is not None
        ]
        accelerator_memory = [
            snapshot.memory_used_bytes
            for snapshot in accelerator_snapshots
            if snapshot.memory_used_bytes is not None
        ]
        return CandidateBenchmarkReport(
            candidate=candidate,
            run=BenchmarkRunEvidence(
                run_id=run_id,
                configuration_sha256=configuration_sha256,
            ),
            evaluation_set_id=evaluation_set.evaluation_set_id,
            evaluation_set_version=evaluation_set.version,
            evaluation_set_sha256=evaluation_set.content_sha256,
            execution_config_sha256=execution_config.evidence_sha256,
            evaluation_purpose=evaluation_set.purpose,
            case_results=results,
            weighted_quality_score=quality,
            task_pass_rate=pass_rate,
            completion_rate=completion_rate,
            mean_latency_ms=fmean(latencies) if latencies else None,
            p95_latency_ms=(
                ModelBenchmarkRunner._nearest_rank(latencies, 0.95)
                if latencies
                else None
            ),
            peak_cpu_percent=max(
                (float(snapshot.cpu_percent) for snapshot in resource_snapshots),
                default=None,
            ),
            peak_memory_percent=max(
                (float(snapshot.memory_percent) for snapshot in resource_snapshots),
                default=None,
            ),
            min_available_memory_bytes=min(
                (snapshot.available_memory_bytes for snapshot in resource_snapshots),
                default=None,
            ),
            peak_accelerator_percent=max(utilization, default=None),
            peak_accelerator_memory_bytes=max(accelerator_memory, default=None),
        )

    @staticmethod
    def _nearest_rank(values: Sequence[float], percentile: float) -> float:
        if not values:
            raise ValueError("percentile requires at least one value")
        if not 0 < percentile <= 1:
            raise ValueError("percentile must be in (0, 1]")
        ordered = sorted(float(value) for value in values)
        index = max(0, ceil(percentile * len(ordered)) - 1)
        return ordered[index]
