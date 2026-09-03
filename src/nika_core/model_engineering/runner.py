from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable, Sequence
from math import ceil, isfinite
from statistics import fmean
from time import perf_counter
from typing import Protocol

from nika_core.model_engineering.contracts import (
    AcceleratorObserverPort,
    AcceleratorSnapshot,
    BenchmarkSuiteReport,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationCase,
    EvaluationSet,
    ModelCandidate,
)
from nika_core.model_gateway.contracts import (
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
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


class ModelBenchmarkRunner:
    def __init__(
        self,
        gateway: ModelCompletionPort,
        *,
        scorer: ModelScoringPort | None = None,
        resource_observer: ResourceObserverPort | None = None,
        accelerator_observer: AcceleratorObserverPort | None = None,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._gateway = gateway
        self._scorer = scorer or ExactMatchScorer()
        self._resource_observer = resource_observer
        self._accelerator_observer = accelerator_observer
        self._clock = clock

    async def benchmark(
        self,
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        *,
        timeout_seconds: float = 60.0,
        temperature: float | None = 0.0,
    ) -> CandidateBenchmarkReport:
        if isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must not be boolean")
        if not isfinite(float(timeout_seconds)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if temperature is not None:
            if isinstance(temperature, bool):
                raise ValueError("temperature must not be boolean")
            temperature_number = float(temperature)
            if not isfinite(temperature_number) or not 0 <= temperature_number <= 2:
                raise ValueError("temperature must be finite and in [0, 2]")

        results: list[CaseBenchmarkResult] = []
        for case in evaluation_set.cases:
            results.append(
                await self._run_case(
                    candidate,
                    evaluation_set,
                    case,
                    timeout_seconds=float(timeout_seconds),
                    temperature=temperature,
                )
            )
        return self._build_report(candidate, evaluation_set, tuple(results))

    async def benchmark_suite(
        self,
        candidates: Sequence[ModelCandidate],
        evaluation_set: EvaluationSet,
        *,
        timeout_seconds: float = 60.0,
        temperature: float | None = 0.0,
    ) -> BenchmarkSuiteReport:
        if not candidates:
            raise ValueError("benchmark suite requires at least one candidate")
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark suite candidate IDs must be unique")

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
            reports=tuple(reports),
        )

    async def _run_case(
        self,
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        case: EvaluationCase,
        *,
        timeout_seconds: float,
        temperature: float | None,
    ) -> CaseBenchmarkResult:
        resource_before = self._resource_snapshot()
        accelerator_before = self._accelerator_snapshot()
        started = self._clock()
        request = ModelRequest(
            request_id=self._request_id(candidate, evaluation_set, case),
            messages=case.messages,
            model=candidate.request_model,
            provider_id=candidate.provider_id,
            privacy=evaluation_set.privacy,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            metadata={
                "evaluation_set_id": evaluation_set.evaluation_set_id,
                "evaluation_set_version": evaluation_set.version,
                "evaluation_set_sha256": evaluation_set.content_sha256,
                "evaluation_case_id": case.case_id,
                "model_candidate_id": candidate.candidate_id,
            },
        )
        try:
            response = await self._gateway.complete(request)
        except ModelGatewayError as error:
            latency_ms = self._elapsed_ms(started)
            resource_after = self._resource_snapshot()
            accelerator_after = self._accelerator_snapshot()
            return CaseBenchmarkResult(
                candidate_id=candidate.candidate_id,
                case_id=case.case_id,
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

        latency_ms = self._elapsed_ms(started)
        resource_after = self._resource_snapshot()
        accelerator_after = self._accelerator_snapshot()
        self._validate_response_identity(candidate, request, response)
        score = float(self._scorer.score(case, response))
        if not isfinite(score) or not 0 <= score <= 1:
            raise ModelBenchmarkError("scorer returned a non-finite or out-of-range score")
        input_tokens, output_tokens, total_tokens = self._usage(response)
        return CaseBenchmarkResult(
            candidate_id=candidate.candidate_id,
            case_id=case.case_id,
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
        elapsed = (finished - started) * 1000.0
        if not isfinite(elapsed) or elapsed < 0:
            raise ModelBenchmarkError("benchmark clock moved backwards or became non-finite")
        return elapsed

    @staticmethod
    def _request_id(
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        case: EvaluationCase,
    ) -> str:
        raw = (
            f"nika-model-benchmark-v1\0{candidate.evidence_sha256}\0"
            f"{evaluation_set.content_sha256}\0{case.case_id}"
        ).encode("utf-8")
        return f"model-bench-{hashlib.sha256(raw).hexdigest()[:32]}"

    @staticmethod
    def _validate_response_identity(
        candidate: ModelCandidate,
        request: ModelRequest,
        response: ModelResponse,
    ) -> None:
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
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
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
        if isinstance(snapshot.cpu_percent, bool) or isinstance(
            snapshot.memory_percent, bool
        ):
            raise ModelBenchmarkError("resource observer returned boolean percentages")
        cpu = float(snapshot.cpu_percent)
        memory = float(snapshot.memory_percent)
        available = snapshot.available_memory_bytes
        if not isfinite(cpu) or not 0 <= cpu <= 100:
            raise ModelBenchmarkError("resource observer returned invalid CPU percent")
        if not isfinite(memory) or not 0 <= memory <= 100:
            raise ModelBenchmarkError("resource observer returned invalid memory percent")
        if isinstance(available, bool) or not isinstance(available, int) or available < 0:
            raise ModelBenchmarkError("resource observer returned invalid available memory")
        return snapshot

    def _accelerator_snapshot(self) -> AcceleratorSnapshot | None:
        if self._accelerator_observer is None:
            return None
        try:
            snapshot = self._accelerator_observer.snapshot()
        except ValueError as exc:
            raise ModelBenchmarkError("accelerator observer returned invalid telemetry") from exc
        if not isinstance(snapshot, AcceleratorSnapshot):
            raise ModelBenchmarkError("accelerator observer returned an invalid snapshot type")
        return snapshot

    @staticmethod
    def _build_report(
        candidate: ModelCandidate,
        evaluation_set: EvaluationSet,
        results: tuple[CaseBenchmarkResult, ...],
    ) -> CandidateBenchmarkReport:
        case_by_id = {case.case_id: case for case in evaluation_set.cases}
        total_weight = sum(float(case.weight) for case in evaluation_set.cases)
        quality = sum(
            result.score * float(case_by_id[result.case_id].weight)
            for result in results
        ) / total_weight
        pass_rate = sum(result.passed for result in results) / len(results)
        completion_rate = (
            sum(result.completion_succeeded for result in results) / len(results)
        )
        latencies = [result.latency_ms for result in results]
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
            evaluation_set_id=evaluation_set.evaluation_set_id,
            evaluation_set_version=evaluation_set.version,
            evaluation_set_sha256=evaluation_set.content_sha256,
            evaluation_purpose=evaluation_set.purpose,
            case_results=results,
            weighted_quality_score=quality,
            task_pass_rate=pass_rate,
            completion_rate=completion_rate,
            mean_latency_ms=fmean(latencies),
            p95_latency_ms=ModelBenchmarkRunner._nearest_rank(latencies, 0.95),
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
